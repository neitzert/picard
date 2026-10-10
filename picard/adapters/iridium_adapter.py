"""Iridium SBD adapter — stores chunks via satellite.

Iridium Short Burst Data works EVERYWHERE on Earth — open ocean,
polar regions, deserts, airborne.  66 satellites provide continuous
global coverage.  340 bytes per message, but it's the ultimate
air-gapped storage channel.

Requires: Iridium SBD modem or gateway API account
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
import aiohttp


class IridiumAdapter(Adapter):
    """Scatter chunks via Iridium satellite SBD messages."""

    def __init__(
        self,
        *,
        imei: str,
        api_url: str = "https://isatdata.pro/api",
        username: str,
        password: str,
        max_chunk_bytes: int = 340,
    ) -> None:
        self._imei = imei
        self._api_url = api_url.rstrip("/")
        self._username = username
        self._password = password
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
            name="iridium",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=2,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        session = await self._get_session()
        payload = {
            "imei": self._imei,
            "data": encoded_chunk,
            "priority": 1,
        }
        auth = aiohttp.BasicAuth(self._username, self._password)
        async with session.post(
            f"{self._api_url}/send_message",
            json=payload,
            auth=auth,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            msg_id = result.get("message_id", uuid.uuid4().hex)

        loc = f"iridium://{self._imei}/{msg_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="iridium", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        cached = self._cache.get(ref.location)
        if cached is not None:
            return cached

        msg_id = ref.location.rsplit("/", 1)[-1]
        session = await self._get_session()
        auth = aiohttp.BasicAuth(self._username, self._password)
        async with session.get(
            f"{self._api_url}/check_messages",
            params={"imei": self._imei, "message_id": msg_id},
            auth=auth,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            messages = result.get("messages", [])
            for msg in messages:
                if str(msg.get("message_id")) == msg_id:
                    return msg["data"]
            raise KeyError(f"message not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="iridium", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            auth = aiohttp.BasicAuth(self._username, self._password)
            async with session.get(
                f"{self._api_url}/status",
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=10),
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
