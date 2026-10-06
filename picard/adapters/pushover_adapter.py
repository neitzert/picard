"""Pushover adapter — stores chunks as push notifications.

Data arrives on the target device as a push notification.

Requires: pip install picard[pushover]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_URL = "https://api.pushover.net"


class PushoverAdapter(Adapter):
    """Scatter chunks into Pushover push notifications."""

    def __init__(
        self,
        app_token: str,
        user_key: str,
        *,
        device: str | None = None,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._app_token = app_token
        self._user_key = user_key
        self._device = device
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pushover",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
            ttl_seconds=30 * 86400,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        chunk_id = uuid.uuid4().hex[:12]
        payload = {
            "token": self._app_token,
            "user": self._user_key,
            "title": f"PICARD:{chunk_id}",
            "message": encoded_chunk,
            "priority": -2,
        }
        if self._device:
            payload["device"] = self._device

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API_URL}/1/messages.json",
                data=payload,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        receipt = data.get("request", chunk_id)
        location = f"pushover://{self._user_key}/{receipt}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="pushover", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="pushover", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API_URL}/1/users/validate.json",
                    data={"token": self._app_token, "user": self._user_key},
                ) as resp:
                    data = await resp.json()
                    return data.get("status") == 1
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
