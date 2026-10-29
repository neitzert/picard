"""Segment event adapter -- stores chunks as Segment track event properties.

Each write_chunk sends a track event with the encoded chunk in the
properties object via the Segment HTTP Tracking API.

Requires: aiohttp, Segment write key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "segment-event"
_BASE = "https://api.segment.io/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class SegmentEventAdapter(Adapter):

    def __init__(self, *, write_key: str = "") -> None:
        self._write_key = write_key
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp, base64
            creds = base64.b64encode(f"{self._write_key}:".encode()).decode()
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "segment-write-key",
                  "substrate": "segment-track-event"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "userId": f"picard:{token}",
            "event": "picard_chunk_write",
            "properties": {
                "token": token,
                "chunk": encoded_chunk,
                "manifest": manifest_id,
            },
        }
        async with session.post(f"{_BASE}/track", json=payload) as resp:
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
            f"segment-event cold read not supported: {loc['token']}")

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
        if not self._write_key:
            return False
        try:
            session = await self._get_session()
            payload = {
                "userId": "picard:health",
                "event": "picard_health_check",
                "properties": {},
            }
            async with session.post(f"{_BASE}/track", json=payload) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
