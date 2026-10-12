"""Discord Webhook adapter — stores chunks via Discord webhooks.

No auth needed to write — the webhook URL is the credential.
Read-back requires a bot token.

Requires: pip install picard[discord-webhook]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class DiscordWebhookAdapter(Adapter):
    """Scatter chunks into Discord via webhook URLs."""

    def __init__(
        self,
        webhook_url: str,
        *,
        read_token: str | None = None,
        max_chunk_bytes: int = 6000,
    ) -> None:
        self._webhook_url = webhook_url
        self._read_token = read_token
        self._max = max_chunk_bytes
        self._webhook_id = webhook_url.rstrip("/").split("/")[-2] if "/" in webhook_url else "unknown"
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
            name="discord-webhook",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        content = f"PICARD:{chunk_id}\n{encoded_chunk}"

        session = await self._get_session()
        async with session.post(
            f"{self._webhook_url}?wait=true",
            json={"content": content},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        msg_id = data["id"]
        location = f"discord-wh://{self._webhook_id}/{msg_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="discord-webhook", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        if not self._read_token:
            raise RuntimeError("read requires read_token (bot token)")

        msg_id = ref.location.rsplit("/", 1)[-1]
        channel_id = ref.location.split("/")[-2]

        session = await self._get_session()
        async with session.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}",
            headers={"Authorization": f"Bot {self._read_token}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        text = data["content"]
        return text.split("\n", 1)[1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        try:
            msg_id = ref.location.rsplit("/", 1)[-1]
            session = await self._get_session()
            async with session.delete(
                f"{self._webhook_url}/messages/{msg_id}",
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="discord-webhook", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(self._webhook_url) as resp:
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
