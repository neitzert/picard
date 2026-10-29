"""Mixpanel event adapter -- stores chunks as Mixpanel track events.

Each write_chunk sends the encoded chunk as a Mixpanel event property.
Mixpanel is an append-only event store -- cold reads raise RuntimeError.
Warm reads work via in-process registry.

Requires: aiohttp, a Mixpanel project token.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mixpanel-event"
_TRACK_URL = "https://api.mixpanel.com/track"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MixpanelEventAdapter(Adapter):

    def __init__(self, *, project_token: str = "") -> None:
        self._project_token = project_token
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
            max_chunk_bytes=4096,
            persistent=False,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,mixpanel-project-token", "substrate": "mixpanel-event-property",
                  "warm-read-only": "true"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        event = {
            "event": "picard_chunk",
            "properties": {
                "token": self._project_token,
                "distinct_id": "picard",
                "chunk_token": token,
                "chunk": encoded_chunk,
                "manifest": manifest_id,
            },
        }
        encoded = base64.b64encode(json.dumps(event).encode()).decode()
        async with session.get(
                f"{_TRACK_URL}?data={encoded}&ip=0") as resp:
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
            "mixpanel-event: cold read not supported (append-only event store)")

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
        if not self._project_token:
            return False
        try:
            session = await self._get_session()
            event = {
                "event": "picard_health",
                "properties": {
                    "token": self._project_token,
                    "distinct_id": "picard_health",
                },
            }
            encoded = base64.b64encode(json.dumps(event).encode()).decode()
            async with session.get(
                    f"{_TRACK_URL}?data={encoded}&ip=0") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
