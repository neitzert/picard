"""Freshdesk adapter -- stores chunks as ticket notes.

Data is stored inside Freshdesk ticket private notes.

Requires: pip install picard[freshdesk]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp


class FreshdeskAdapter(Adapter):
    """Scatter chunks into Freshdesk ticket notes."""

    def __init__(
        self,
        domain: str,
        api_key: str,
        *,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._domain = domain
        self._api_key = api_key
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="freshdesk",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=40,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "subject": f"PICARD:{chunk_id}",
            "description": encoded_chunk,
            "email": "picard@store.local",
            "priority": 1,
            "status": 2,
            "tags": ["picard", "store"],
        }
        auth = aiohttp.BasicAuth(self._api_key, "X")
        headers = {"Content-Type": "application/json"}

        session = await self._get_session()
        async with session.post(
            f"https://{self._domain}.freshdesk.com/api/v2/tickets",
            json=payload,
            headers=headers,
            auth=auth,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        ticket_id = data.get("id", chunk_id)
        location = f"freshdesk://{self._domain}/tickets/{ticket_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="freshdesk", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="freshdesk", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            auth = aiohttp.BasicAuth(self._api_key, "X")
            session = await self._get_session()
            async with session.get(
                f"https://{self._domain}.freshdesk.com/api/v2/tickets?per_page=1",
                auth=auth,
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
