"""Slack Webhook adapter — stores chunks via Slack Incoming Webhooks.

Write-only via webhook URL (no OAuth needed). Read-back requires
a separate API token for conversations.history.

Requires: pip install picard-store[slack-webhook]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SlackWebhookAdapter(Adapter):
    """Scatter chunks into Slack via incoming webhooks."""

    def __init__(
        self,
        webhook_url: str,
        *,
        channel: str | None = None,
        read_api_token: str | None = None,
        max_chunk_bytes: int = 40_000,
    ) -> None:
        self._webhook_url = webhook_url
        self._channel = channel
        self._read_token = read_api_token
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
            name="slack-webhook",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        marker = f"PICARD:{chunk_id}"
        payload = {"text": f"{marker}\n{encoded_chunk}"}
        if self._channel:
            payload["channel"] = self._channel

        session = await self._get_session()
        async with session.post(self._webhook_url, json=payload) as resp:
            resp.raise_for_status()

        location = f"slack-wh://{self._channel or 'default'}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="slack-webhook", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        if not self._read_token or not self._channel:
            raise RuntimeError("read requires read_api_token and channel")

        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"PICARD:{chunk_id}"

        session = await self._get_session()
        async with session.post(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {self._read_token}"},
            json={"channel": self._channel, "limit": 200},
        ) as resp:
            data = await resp.json()

        for msg in data.get("messages", []):
            text = msg.get("text", "")
            if text.startswith(marker):
                return text.split("\n", 1)[1]

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="slack-webhook", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.post(
                self._webhook_url,
                json={"text": "picard health check"},
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
