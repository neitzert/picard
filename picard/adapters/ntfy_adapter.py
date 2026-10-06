"""ntfy adapter — stores chunks via ntfy.sh push notifications.

Zero signup required — pick a topic name and start sending.

Requires: pip install picard[ntfy]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class NtfyAdapter(Adapter):
    """Scatter chunks into ntfy.sh push notifications."""

    def __init__(
        self,
        topic: str,
        *,
        server: str = "https://ntfy.sh",
        auth_token: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._topic = topic
        self._server = server.rstrip("/")
        self._auth_token = auth_token
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._auth_token:
            h["Authorization"] = f"Bearer {self._auth_token}"
        return h

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ntfy",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
            ttl_seconds=43200,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        chunk_id = uuid.uuid4().hex[:12]
        headers = self._headers()
        headers["X-Title"] = f"PICARD:{chunk_id}"
        headers["X-Tags"] = "picard"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._server}/{self._topic}",
                headers=headers,
                data=encoded_chunk.encode(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        msg_id = data.get("id", chunk_id)
        location = f"ntfy://{self._server}/{self._topic}/{msg_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="ntfy", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._server}/{self._topic}/json",
                headers=self._headers(),
                params={"poll": "1"},
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()

        import json

        msg_id = ref.location.rsplit("/", 1)[-1]
        for line in text.strip().split("\n"):
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("id") == msg_id:
                return msg.get("message", "")

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ntfy", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._server}/v1/health",
                    headers=self._headers(),
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
