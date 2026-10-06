"""Microsoft Teams Webhook adapter — stores chunks via Teams Incoming Webhooks.

Write via webhook URL (Adaptive Card). Read-back requires Microsoft Graph API token.

Requires: pip install picard[teams-webhook]  (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class TeamsWebhookAdapter(Adapter):
    """Scatter chunks into Microsoft Teams via incoming webhooks."""

    def __init__(
        self,
        webhook_url: str,
        *,
        read_api_token: str | None = None,
        team_id: str | None = None,
        channel_id: str | None = None,
        max_chunk_bytes: int = 28_000,
    ) -> None:
        self._webhook_url = webhook_url
        self._read_token = read_api_token
        self._team_id = team_id
        self._channel_id = channel_id
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="teams-webhook",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        chunk_id = uuid.uuid4().hex[:12]
        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": f"PICARD:{chunk_id}"},
                            {"type": "TextBlock", "text": encoded_chunk, "wrap": True},
                        ],
                    },
                }
            ],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self._webhook_url, json=card) as resp:
                resp.raise_for_status()

        location = f"teams://{self._team_id or 'default'}/{self._channel_id or 'default'}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="teams-webhook", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        if not self._read_token or not self._team_id or not self._channel_id:
            raise RuntimeError("read requires read_api_token, team_id, and channel_id")

        import aiohttp

        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"PICARD:{chunk_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://graph.microsoft.com/v1.0/teams/{self._team_id}/channels/{self._channel_id}/messages",
                headers={"Authorization": f"Bearer {self._read_token}"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        for msg in data.get("value", []):
            body = msg.get("body", {}).get("content", "")
            if marker in body:
                lines = body.split("\n")
                for i, line in enumerate(lines):
                    if marker in line and i + 1 < len(lines):
                        return lines[i + 1]

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="teams-webhook", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json={"type": "message", "attachments": []},
                ) as resp:
                    return resp.status in (200, 202)
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
