"""Google Cloud Storage adapter — stores chunks as GCS objects.

Each chunk becomes an object in a GCS bucket, keyed by manifest/block/chunk.

Requires: pip install picard-store[gcs]   (google-auth, aiohttp)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GcsAdapter(Adapter):
    """Scatter chunks into Google Cloud Storage."""

    def __init__(
        self,
        bucket: str,
        *,
        credentials_json: str | None = None,
        project_id: str | None = None,
        prefix: str = "picard/",
        max_chunk_bytes: int = 5_242_880,
    ) -> None:
        self._bucket = bucket
        self._credentials_json = credentials_json
        self._project_id = project_id
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}.pchk"

    def _get_headers(self) -> dict[str, str]:
        try:
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default()
            creds.refresh(google.auth.transport.requests.Request())
            return {"Authorization": f"Bearer {creds.token}"}
        except Exception:
            return {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gcs",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            key = f"{self._prefix}{uuid.uuid4().hex}.pchk"

        headers = await asyncio.to_thread(self._get_headers)
        headers["Content-Type"] = "text/plain"
        url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/"
            f"{self._bucket}/o?uploadType=media&name={key}"
        )

        session = await self._get_session()
        async with session.post(
            url, data=encoded_chunk.encode(), headers=headers
        ) as resp:
            resp.raise_for_status()

        return StoredChunkRef(adapter_name="gcs", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        headers = await asyncio.to_thread(self._get_headers)
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{self._bucket}/o/{ref.location}?alt=media"
        )

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        headers = await asyncio.to_thread(self._get_headers)
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{self._bucket}/o/{ref.location}"
        )

        try:
            session = await self._get_session()
            async with session.delete(url, headers=headers) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        headers = await asyncio.to_thread(self._get_headers)
        prefix = f"{self._prefix}{manifest_id}/"
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{self._bucket}/o?prefix={prefix}"
        )
        refs: list[StoredChunkRef] = []

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            for item in data.get("items", []):
                refs.append(
                    StoredChunkRef(adapter_name="gcs", location=item["name"])
                )

        return refs

    async def health_check(self) -> bool:
        headers = await asyncio.to_thread(self._get_headers)
        url = f"https://storage.googleapis.com/storage/v1/b/{self._bucket}"

        try:
            session = await self._get_session()
            async with session.get(url, headers=headers) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
