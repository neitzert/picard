"""Telegram Bot adapter -- stores chunks as Telegram messages sent via the Bot API."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "telegram-bot"
_BASE = "https://api.telegram.org"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class TelegramBotAdapter(Adapter):

    def __init__(self, *, bot_token: str = "", chat_id: str = "") -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._session = None
        self._reg: dict[str, str] = {}

    def _api(self, method: str) -> str:
        return f"{_BASE}/bot{self._bot_token}/{method}"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "aiohttp", "substrate": "telegram-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        self._reg[token] = encoded_chunk
        message_id: int | None = None
        if self._bot_token and self._chat_id:
            session = await self._get_session()
            payload = {
                "chat_id": self._chat_id,
                "text": f"PICARD:{token}:{encoded_chunk}",
            }
            try:
                async with session.post(self._api("sendMessage"),
                                        json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                message_id = data.get("result", {}).get("message_id")
            except Exception:
                pass
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "message_id": message_id,
                                 "chunk": encoded_chunk,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        chunk = loc.get("chunk", "")
        if chunk:
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"telegram-bot chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        msg_id = loc.get("message_id")
        if self._bot_token and self._chat_id and msg_id:
            session = await self._get_session()
            try:
                async with session.post(
                        self._api("deleteMessage"),
                        json={"chat_id": self._chat_id,
                              "message_id": msg_id}) as resp:
                    pass
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "message_id": None,
                                         "chunk": chunk,
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._bot_token:
            return True
        try:
            session = await self._get_session()
            async with session.get(self._api("getMe")) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
