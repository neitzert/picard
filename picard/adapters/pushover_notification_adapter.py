"""Pushover notification adapter -- stores chunks as Pushover push notifications.

Each write_chunk delivers the encoded chunk as a notification message.
Pushover is a fire-and-forget service -- cold reads raise RuntimeError.
Warm reads work via in-process registry.

Requires: aiohttp, a Pushover app token and user key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "pushover-notification"
_API_URL = "https://api.pushover.net/1/messages.json"
_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class PushoverNotificationAdapter(Adapter):

    def __init__(self, *, app_token: str = "", user_key: str = "") -> None:
        self._app_token = app_token
        self._user_key = user_key
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1024,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,pushover-credentials", "substrate": "pushover-notification",
                  "warm-read-only": "true"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        data = {
            "token": self._app_token,
            "user": self._user_key,
            "message": encoded_chunk,
            "title": f"picard-{token}",
        }
        async with session.post(_API_URL, data=data) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        raise RuntimeError(
            "pushover-notification: cold read not supported (fire-and-forget substrate)")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._app_token or not self._user_key:
            return False
        try:
            session = await self._get_session()
            data = {"token": self._app_token, "user": self._user_key}
            async with session.post(_VALIDATE_URL, data=data) as resp:
                body = await resp.json()
                return body.get("status") == 1
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
