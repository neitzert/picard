"""Twitch adapter — stores chunks in channel descriptions.

Updates the channel description field via the Helix API.  Each chunk
overwrites the description, so only one chunk per broadcaster.
Use chat messages for multi-chunk storage (ephemeral).

Requires: pip install picard[twitch]   (uses aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_API_BASE = "https://api.twitch.tv/helix"


class TwitchAdapter(Adapter):
    """Scatter chunks into Twitch channel descriptions or chat."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        broadcaster_id: str,
        *,
        use_chat: bool = False,
        max_chunk_bytes: int = 300,
    ) -> None:
        self._client_id = client_id
        self._access_token = access_token
        self._broadcaster_id = broadcaster_id
        self._use_chat = use_chat
        self._max = max_chunk_bytes
        self._chat_cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="twitch",
            max_chunk_bytes=self._max,
            persistent=not self._use_chat,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp

        if self._use_chat:
            loc = f"twitch://{self._broadcaster_id}/chat/{uuid.uuid4().hex[:8]}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{_API_BASE}/chat/messages",
                    headers=self._headers(),
                    json={
                        "broadcaster_id": self._broadcaster_id,
                        "sender_id": self._broadcaster_id,
                        "message": encoded_chunk,
                    },
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    msg_id = data.get("data", [{}])[0].get("message_id", uuid.uuid4().hex)
                    loc = f"twitch://{self._broadcaster_id}/chat/{msg_id}"
            self._chat_cache[loc] = encoded_chunk
            return StoredChunkRef(adapter_name="twitch", location=loc)

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{_API_BASE}/channels",
                headers=self._headers(),
                params={"broadcaster_id": self._broadcaster_id},
                json={"description": encoded_chunk},
            ) as resp:
                resp.raise_for_status()
        loc = f"twitch://{self._broadcaster_id}/description"
        return StoredChunkRef(adapter_name="twitch", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiohttp

        if "/chat/" in ref.location:
            cached = self._chat_cache.get(ref.location)
            if cached:
                return cached
            raise KeyError(f"chat message not in cache: {ref.location}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/channels",
                headers=self._headers(),
                params={"broadcaster_id": self._broadcaster_id},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data["data"][0]["description"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiohttp

        if "/chat/" in ref.location:
            return self._chat_cache.pop(ref.location, None) is not None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(
                    f"{_API_BASE}/channels",
                    headers=self._headers(),
                    params={"broadcaster_id": self._broadcaster_id},
                    json={"description": ""},
                ) as resp:
                    return resp.status == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._chat_cache.items():
            if manifest_id in data:
                refs.append(StoredChunkRef(adapter_name="twitch", location=loc))
        return refs

    async def health_check(self) -> bool:
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{_API_BASE}/users",
                    headers=self._headers(),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
