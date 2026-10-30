"""OpsGenie adapter -- stores chunks as alert details.

Data is stored inside OpsGenie alert fields.

Requires: pip install picard-store[opsgenie]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_URL = "https://api.opsgenie.com/v2"


class OpsGenieAdapter(Adapter):
    """Scatter chunks into OpsGenie alerts."""

    def __init__(
        self,
        api_key: str,
        *,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._api_key = api_key
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
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

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="opsgenie",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"GenieKey {self._api_key}",
            "Content-Type": "application/json",
        }

    def _manifest_id_from_chunk(self, chunk: str) -> str:
        try:
            parts = chunk.split(":")
            if parts[0] == "PCHK" and len(parts) >= 3:
                return parts[2]
        except Exception:
            pass
        return ""

    async def _resolve_alert_id(self, request_id: str) -> str:
        """Resolve an async OpsGenie requestId to the actual alert ID."""
        import asyncio
        session = await self._get_session()
        for _ in range(5):
            async with session.get(
                f"{_API_URL}/alerts/requests/{request_id}",
                headers=self._headers(),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    alert_id = data.get("data", {}).get("alertId", "")
                    if alert_id:
                        return alert_id
            await asyncio.sleep(0.5)
        raise RuntimeError(f"opsgenie requestId {request_id} did not resolve to an alert")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "message": f"PICARD:{chunk_id}",
            "description": encoded_chunk,
            "priority": "P5",
            "tags": ["picard", "store"],
        }

        session = await self._get_session()
        async with session.post(
            f"{_API_URL}/alerts",
            json=payload,
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        request_id = data.get("requestId", chunk_id)
        alert_id = await self._resolve_alert_id(request_id)
        location = f"opsgenie://alerts/{alert_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="opsgenie", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        alert_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        async with session.get(
            f"{_API_URL}/alerts/{alert_id}",
            headers=self._headers(),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data.get("data", {}).get("description", "")
        if not chunk:
            raise KeyError(f"opsgenie alert {alert_id} has no description")
        self._cache[ref.location] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        alert_id = ref.location.rsplit("/", 1)[-1]
        try:
            session = await self._get_session()
            async with session.post(
                f"{_API_URL}/alerts/{alert_id}/close",
                json={"note": "deleted by picard"},
                headers=self._headers(),
            ) as resp:
                return resp.status in (200, 202, 404)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="opsgenie", location=loc)
            for loc, chunk in self._cache.items()
            if self._manifest_id_from_chunk(chunk) == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            headers = {"Authorization": f"GenieKey {self._api_key}"}
            session = await self._get_session()
            async with session.get(
                f"{_API_URL}/heartbeats",
                headers=headers,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
