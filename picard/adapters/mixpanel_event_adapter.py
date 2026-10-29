"""Mixpanel event adapter -- stores chunks as Mixpanel event properties.

Each write_chunk tracks an event via the Mixpanel Ingestion API with the
encoded chunk stored in event properties. Cold reads use in-memory cache.

Requires: aiohttp, Mixpanel project token.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mixpanel-event"
_URL = "https://api.mixpanel.com/track"


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
        self._token = project_token
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=2048,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "mixpanel-project-token",
                  "substrate": "mixpanel-track-event"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        event = {
            "event": "picard_chunk",
            "properties": {
                "token": self._token,
                "distinct_id": f"picard:{token}",
                "picard_token": token,
                "picard_chunk": encoded_chunk,
                "picard_manifest": manifest_id,
                "time": int(time.time()),
            },
        }
        data = base64.b64encode(json.dumps(event).encode()).decode()
        session = await self._get_session()
        async with session.get(_URL, params={"data": data}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        raise KeyError(
            f"mixpanel-event cold read not supported: {loc['token']}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t,
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        return bool(self._token)

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
