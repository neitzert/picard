"""Telegram adapter — stores chunks as bot messages in a chat/channel.

Each chunk becomes a message sent by a Telegram bot. The message_id
is the chunk reference. Reading uses forwardMessage to retrieve the
text (Bot API has no direct getMessage), then deletes the forward.

Requires: pip install picard[telegram]  (uses httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class TelegramAdapter(Adapter):
    """Scatter chunks into Telegram messages via the Bot API."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        *,
        max_chunk_bytes: int = 4000,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = str(chat_id)
        self._max = max_chunk_bytes
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="telegram",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=20,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        resp = await client.post(
            f"{self._base}/sendMessage",
            json={"chat_id": self._chat_id, "text": encoded_chunk},
        )
        data = resp.json()
        message_id = data["result"]["message_id"]
        return StoredChunkRef(adapter_name="telegram", location=str(message_id))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        fwd = await client.post(
            f"{self._base}/forwardMessage",
            json={
                "chat_id": self._chat_id,
                "from_chat_id": self._chat_id,
                "message_id": int(ref.location),
                "disable_notification": True,
            },
        )
        fwd_data = fwd.json()
        text = fwd_data["result"]["text"]
        fwd_id = fwd_data["result"]["message_id"]
        await client.post(
            f"{self._base}/deleteMessage",
            json={"chat_id": self._chat_id, "message_id": fwd_id},
        )
        return text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self._base}/deleteMessage",
                json={"chat_id": self._chat_id, "message_id": int(ref.location)},
            )
            return resp.json().get("result", False) is True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # Bot API has no message history endpoint; recovery requires
        # the local index or a Telegram userbot / TDLib client.
        return []

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.post(f"{self._base}/getMe")
            return resp.json().get("ok", False) is True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
