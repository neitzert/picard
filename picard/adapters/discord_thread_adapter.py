"""Discord thread adapter -- stores chunks as messages in Discord forum threads.

Each write_chunk creates a forum thread with the encoded chunk as the
initial message. Requires a forum channel ID.

Requires: aiohttp, Discord Bot token, forum channel_id.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "discord-thread"
_BASE = "https://discord.com/api/v10"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class DiscordThreadAdapter(Adapter):

    def __init__(self, *, bot_token: str = "",
                 channel_id: str = "") -> None:
        self._token = bot_token
        self._channel_id = channel_id
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=2000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=50,
            tags={"requires": "discord-bot-token,forum-channel-id",
                  "substrate": "discord-forum-thread"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "name": f"picard:{manifest_id[:16]}:{token}",
            "message": {"content": encoded_chunk},
        }
        async with session.post(
                f"{_BASE}/channels/{self._channel_id}/threads",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        thread_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "thread_id": thread_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE}/channels/{loc['thread_id']}/messages",
                params={"limit": "1"}) as resp:
            resp.raise_for_status()
            msgs = await resp.json()
        if not msgs:
            raise KeyError(f"discord-thread chunk not found: {loc['token']}")
        chunk = msgs[0]["content"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE}/channels/{loc['thread_id']}") as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._token or not self._channel_id:
            return []
        session = await self._get_session()
        prefix = f"picard:{manifest_id[:16]}:"
        refs = []
        before = None
        while True:
            params: dict = {"limit": "100"}
            if before:
                params["before"] = before
            async with session.get(
                    f"{_BASE}/channels/{self._channel_id}/threads/archived/public",
                    params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            threads = data.get("threads", [])
            for thread in threads:
                name = thread.get("name", "")
                if name.startswith(prefix):
                    token = name[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "thread_id": thread["id"],
                                              "manifest": manifest_id}),
                    ))
            if not data.get("has_more"):
                break
            if threads:
                before = threads[-1]["id"]
            else:
                break
        return refs

    async def health_check(self) -> bool:
        if not self._token or not self._channel_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE}/users/@me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
