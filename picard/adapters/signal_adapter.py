"""Signal adapter — stores chunks via Signal Messenger (signal-cli REST API).

End-to-end encrypted. Carrier cannot see content.

Requires: signal-cli REST API running, pip install picard-store[signal]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SignalAdapter(Adapter):
    """Scatter chunks into Signal messages."""

    def __init__(
        self,
        phone_number: str,
        recipient: str,
        *,
        api_url: str = "http://localhost:8080",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._phone = phone_number
        self._recipient = recipient
        self._api_url = api_url.rstrip("/")
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
            name="signal",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        message = f"PICARD:{chunk_id}\n{encoded_chunk}"

        session = await self._get_session()
        async with session.post(
            f"{self._api_url}/v2/send",
            json={
                "message": message,
                "number": self._phone,
                "recipients": [self._recipient],
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        ts = data.get("timestamp", chunk_id)
        location = f"signal://{self._phone}/{ts}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="signal", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        session = await self._get_session()
        async with session.get(
            f"{self._api_url}/v1/receive/{self._phone}",
        ) as resp:
            resp.raise_for_status()
            messages = await resp.json()

        ts = ref.location.rsplit("/", 1)[-1]
        for msg in messages:
            if str(msg.get("timestamp")) == ts:
                text = msg.get("envelope", {}).get("dataMessage", {}).get("message", "")
                return text.split("\n", 1)[1]

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="signal", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._api_url}/v1/about") as resp:
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
