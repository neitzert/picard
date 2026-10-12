"""Datadog adapter -- stores chunks as custom events.

Data is embedded inside Datadog event bodies, stored
alongside normal observability traffic.

Requires: pip install picard[datadog]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_URL = "https://api.datadoghq.com/api/v1"


class DatadogAdapter(Adapter):
    """Scatter chunks into Datadog custom events."""

    def __init__(
        self,
        api_key: str,
        *,
        app_key: str | None = None,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._api_key = api_key
        self._app_key = app_key
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
            name="datadog",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "title": f"PICARD:{chunk_id}",
            "text": encoded_chunk,
            "tags": ["picard:store"],
            "alert_type": "info",
            "source_type_name": "my_apps",
        }
        headers = {
            "DD-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }
        if self._app_key:
            headers["DD-APPLICATION-KEY"] = self._app_key

        session = await self._get_session()
        async with session.post(
            f"{_API_URL}/events",
            json=payload,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        event_id = data.get("event", {}).get("id", chunk_id)
        location = f"datadog://events/{event_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="datadog", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="datadog", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            headers = {"DD-API-KEY": self._api_key}
            session = await self._get_session()
            async with session.get(
                f"{_API_URL}/validate",
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
