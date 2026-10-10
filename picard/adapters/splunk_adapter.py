"""Splunk HEC adapter -- stores chunks as events via HTTP Event Collector.

Data is stored through Splunk's HEC endpoint, appearing
as routine log ingestion traffic.

Requires: pip install picard[splunk]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SplunkHECAdapter(Adapter):
    """Scatter chunks into Splunk via HTTP Event Collector."""

    def __init__(
        self,
        hec_url: str,
        hec_token: str,
        *,
        index: str = "main",
        source: str = "picard",
        sourcetype: str = "_json",
        max_chunk_bytes: int = 10000,
        verify_ssl: bool = True,
    ) -> None:
        self._hec_url = hec_url.rstrip("/")
        self._hec_token = hec_token
        self._index = index
        self._source = source
        self._sourcetype = sourcetype
        self._max = max_chunk_bytes
        self._verify_ssl = verify_ssl
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
            name="splunk_hec",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "event": {
                "picard_chunk_id": chunk_id,
                "payload": encoded_chunk,
            },
            "index": self._index,
            "source": self._source,
            "sourcetype": self._sourcetype,
        }
        headers = {
            "Authorization": f"Splunk {self._hec_token}",
            "Content-Type": "application/json",
        }

        ssl = None if self._verify_ssl else False
        session = await self._get_session()
        async with session.post(
            f"{self._hec_url}/services/collector/event",
            json=payload,
            headers=headers,
            ssl=ssl,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        location = f"splunk://{self._index}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="splunk_hec", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="splunk_hec", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            headers = {"Authorization": f"Splunk {self._hec_token}"}
            ssl = None if self._verify_ssl else False
            session = await self._get_session()
            async with session.get(
                f"{self._hec_url}/services/collector/health",
                headers=headers,
                ssl=ssl,
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
