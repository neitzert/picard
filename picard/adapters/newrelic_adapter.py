"""New Relic adapter -- stores chunks as custom events.

Data is stored in New Relic Insights as custom events.

Requires: pip install picard-store[newrelic]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_INSIGHTS_URL = "https://insights-collector.newrelic.com/v1/accounts"


class NewRelicAdapter(Adapter):
    """Scatter chunks into New Relic custom events."""

    def __init__(
        self,
        account_id: str,
        insert_key: str,
        *,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._account_id = account_id
        self._insert_key = insert_key
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
            name="newrelic",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        payload = [
            {
                "eventType": "PicardChunk",
                "chunkId": chunk_id,
                "payload": encoded_chunk,
            }
        ]
        headers = {
            "X-Insert-Key": self._insert_key,
            "Content-Type": "application/json",
        }

        session = await self._get_session()
        async with session.post(
            f"{_INSIGHTS_URL}/{self._account_id}/events",
            json=payload,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        success = data.get("success", False)
        location = f"newrelic://{self._account_id}/events/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="newrelic", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="newrelic", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            headers = {"X-Insert-Key": self._insert_key}
            session = await self._get_session()
            async with session.post(
                f"{_INSIGHTS_URL}/{self._account_id}/events",
                json=[{"eventType": "PicardHealthCheck", "check": True}],
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
