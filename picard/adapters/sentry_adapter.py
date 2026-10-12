"""Sentry adapter — stores chunks as Sentry events.

Each chunk becomes a Sentry event with the data in the message or
extra fields, stored in the error tracking system.

Requires: pip install picard-store[sentry]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence
from urllib.parse import urlparse

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SentryAdapter(Adapter):
    """Scatter chunks as Sentry error events."""

    def __init__(
        self,
        dsn: str,
        *,
        org_slug: str = "",
        project_slug: str = "",
        auth_token: str = "",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._dsn = dsn
        self._org_slug = org_slug
        self._project_slug = project_slug
        self._auth_token = auth_token
        self._max = max_chunk_bytes
        self._store_url, self._public_key = self._parse_dsn(dsn)
        self._api_base = f"https://sentry.io/api/0"
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    @staticmethod
    def _parse_dsn(dsn: str) -> tuple[str, str]:
        parsed = urlparse(dsn)
        public_key = parsed.username or ""
        project_id = parsed.path.strip("/")
        host = parsed.hostname or "sentry.io"
        scheme = parsed.scheme
        store_url = f"{scheme}://{host}/api/{project_id}/store/"
        return store_url, public_key

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="sentry",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _chunk_key(self, encoded_chunk: str) -> tuple[str, str]:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"b{chunk.block_index}_c{chunk.chunk_index}"
            return chunk.manifest_id, key
        except ValueError:
            uid = uuid.uuid4().hex
            return uid, uid

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id, chunk_key = self._chunk_key(encoded_chunk)
        event_id = uuid.uuid4().hex

        event = {
            "event_id": event_id,
            "message": f"picard:{manifest_id}:{chunk_key}",
            "level": "info",
            "platform": "python",
            "extra": {
                "picard_data": encoded_chunk,
                "picard_manifest": manifest_id,
                "picard_key": chunk_key,
            },
            "tags": {
                "picard": "true",
                "picard_manifest": manifest_id,
            },
        }

        headers = {
            "Content-Type": "application/json",
            "X-Sentry-Auth": (
                f"Sentry sentry_version=7, sentry_client=picard/1.0, "
                f"sentry_key={self._public_key}"
            ),
        }

        session = await self._get_session()
        async with session.post(
            self._store_url, json=event, headers=headers
        ) as resp:
            resp.raise_for_status()

        return StoredChunkRef(
            adapter_name="sentry",
            location=f"sentry://{self._project_slug}/{event_id}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        event_id = ref.location.rsplit("/", 1)[-1]
        url = (
            f"{self._api_base}/projects/{self._org_slug}/{self._project_slug}"
            f"/events/{event_id}/"
        )
        headers = {"Authorization": f"Bearer {self._auth_token}"}

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            ctx = data.get("context", {})
            picard_data = ctx.get("picard_data")
            if picard_data is None:
                for entry in data.get("entries", []):
                    if entry.get("type") == "message":
                        extra = entry.get("data", {}).get("params", {})
                        picard_data = extra.get("picard_data")
            if picard_data is None:
                raise KeyError(f"event has no picard data: {event_id}")
            return picard_data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        event_id = ref.location.rsplit("/", 1)[-1]
        url = (
            f"{self._api_base}/projects/{self._org_slug}/{self._project_slug}"
            f"/events/{event_id}/"
        )
        headers = {"Authorization": f"Bearer {self._auth_token}"}
        try:
            session = await self._get_session()
            async with session.delete(url, headers=headers) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = (
            f"{self._api_base}/projects/{self._org_slug}/{self._project_slug}"
            f"/events/"
        )
        headers = {"Authorization": f"Bearer {self._auth_token}"}
        params = {"query": f"picard_manifest:{manifest_id}", "full": "true"}

        try:
            session = await self._get_session()
            async with session.get(
                url, headers=headers, params=params
            ) as resp:
                resp.raise_for_status()
                events = await resp.json()
            return [
                StoredChunkRef(
                    adapter_name="sentry",
                    location=f"sentry://{self._project_slug}/{e['eventID']}",
                )
                for e in events
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._api_base}/",
                headers={"Authorization": f"Bearer {self._auth_token}"},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
