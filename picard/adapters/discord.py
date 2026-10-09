"""Discord adapter — stores chunks as messages via webhook + bot token.

Chunks are posted through a Discord webhook URL.  Reading, deleting,
and listing use the Discord bot API with a bot token that has access
to the target channel.

Requires: pip install picard[cloudflare]   (uses httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://discord.com/api/v10"


class DiscordAdapter(Adapter):
    """Scatter chunks into Discord channel messages."""

    def __init__(
        self,
        webhook_url: str,
        channel_id: str,
        bot_token: str,
        *,
        max_chunk_bytes: int = 1900,
    ) -> None:
        self._webhook_url = webhook_url
        self._channel_id = channel_id
        self._bot_token = bot_token
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bot {self._bot_token}"},
            timeout=30.0,
        )
        return self._client

    def _messages_url(self, message_id: str | None = None) -> str:
        base = f"{_API_BASE}/channels/{self._channel_id}/messages"
        if message_id:
            return f"{base}/{message_id}"
        return base

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="discord",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as wh_client:
            resp = await wh_client.post(
                f"{self._webhook_url}?wait=true",
                json={"content": encoded_chunk},
            )
            resp.raise_for_status()
            data = resp.json()
        return StoredChunkRef(adapter_name="discord", location=data["id"])

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(self._messages_url(ref.location))
        resp.raise_for_status()
        return resp.json()["content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(self._messages_url(ref.location))
            return resp.status_code == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        before: str | None = None

        while True:
            url = self._messages_url()
            params: dict[str, str | int] = {"limit": 100}
            if before:
                params["before"] = before
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            messages = resp.json()

            if not messages:
                break

            for msg in messages:
                content = msg.get("content", "")
                if content.startswith(prefix):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="discord",
                            location=msg["id"],
                        )
                    )

            if len(messages) < 100:
                break
            before = messages[-1]["id"]

        return refs

    async def health_check(self) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{_API_BASE}/channels/{self._channel_id}"
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
