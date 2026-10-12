"""Slack adapter — stores chunks as messages in a Slack channel.

Each chunk becomes a message in the configured channel. The message
text is the PCHK-encoded chunk string. Messages are identified by
their timestamp (ts), which is Slack's unique message ID.

Requires: pip install picard[slack]  (uses httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_BASE_URL = "https://slack.com/api"


class SlackAdapter(Adapter):
    """Scatter chunks into Slack channel messages."""

    def __init__(
        self,
        bot_token: str,
        channel_id: str,
        *,
        max_chunk_bytes: int = 3900,
    ) -> None:
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._bot_token}"},
            timeout=30.0,
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="slack",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        resp = await client.post(
            f"{_BASE_URL}/chat.postMessage",
            json={"channel": self._channel_id, "text": encoded_chunk},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack postMessage failed: {data.get('error')}")
        return StoredChunkRef(adapter_name="slack", location=data["ts"])

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.post(
            f"{_BASE_URL}/conversations.history",
            json={
                "channel": self._channel_id,
                "latest": ref.location,
                "oldest": ref.location,
                "inclusive": True,
                "limit": 1,
            },
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack history failed: {data.get('error')}")
        return data["messages"][0]["text"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{_BASE_URL}/chat.delete",
                json={"channel": self._channel_id, "ts": ref.location},
            )
            return resp.json().get("ok", False)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        cursor: str | None = None

        while True:
            payload: dict = {"channel": self._channel_id, "limit": 200}
            if cursor:
                payload["cursor"] = cursor

            resp = await client.post(
                f"{_BASE_URL}/conversations.history",
                json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                break

            for msg in data.get("messages", []):
                text = msg.get("text", "")
                if text.startswith(prefix):
                    refs.append(
                        StoredChunkRef(adapter_name="slack", location=msg["ts"])
                    )

            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.post(f"{_BASE_URL}/auth.test")
            return resp.json().get("ok", False)
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
