"""Amplitude event adapter -- stores chunks as Amplitude event properties.

Each write_chunk sends an event to Amplitude with the encoded chunk as
an event property. Reads use in-memory cache only (events are immutable).

Requires: aiohttp, Amplitude API key.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "amplitude-event"
_URL = "https://api2.amplitude.com/2/httpapi"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AmplitudeEventAdapter(Adapter):

    def __init__(self, *, api_key: str = "") -> None:
        self._api_key = api_key
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
            max_chunk_bytes=1024,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "amplitude-api-key",
                  "substrate": "amplitude-event-property"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "api_key": self._api_key,
            "events": [{
                "user_id": f"picard:{token}",
                "event_type": "picard_chunk",
                "time": int(time.time() * 1000),
                "event_properties": {
                    "token": token,
                    "chunk": encoded_chunk,
                    "manifest": manifest_id,
                },
            }],
        }
        async with session.post(_URL, json=payload) as resp:
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
            f"amplitude-event cold read not supported: {loc['token']}")

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
        if not self._api_key:
            return False
        return True

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
