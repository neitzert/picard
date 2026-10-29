"""Matrix room adapter -- stores chunks as Matrix room state events.

Each write_chunk sends a custom state event (type: net.picard.chunk) to a
Matrix room. The state key is the token; the content contains the encoded
chunk. Supports any Matrix homeserver via the Client-Server API.

Requires: pip install aiohttp
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "matrix-room"
_EVENT_TYPE = "net.picard.chunk"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MatrixRoomAdapter(Adapter):

    def __init__(
        self,
        *,
        homeserver: str = "https://matrix.org",
        access_token: str = "",
        room_id: str = "",
    ) -> None:
        self._homeserver = homeserver.rstrip("/")
        self._access_token = access_token
        self._room_id = room_id
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=50,
            tags={"requires": "aiohttp", "substrate": "matrix-state-event"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()

        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{self._room_id}/state/{_EVENT_TYPE}/{token}"
        )
        payload = {"chunk": encoded_chunk, "manifest": manifest_id}
        async with session.put(url, json=payload) as resp:
            resp.raise_for_status()

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        session = await self._get_session()
        url = (
            f"{self._homeserver}/_matrix/client/v3/rooms/"
            f"{self._room_id}/state/{_EVENT_TYPE}/{token}"
        )
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()

        chunk = data.get("chunk", "")
        if not chunk:
            raise KeyError(f"matrix-room chunk not found: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        try:
            session = await self._get_session()
            url = (
                f"{self._homeserver}/_matrix/client/v3/rooms/"
                f"{self._room_id}/state/{_EVENT_TYPE}/{token}"
            )
            payload = {"chunk": "", "deleted": True}
            async with session.put(url, json=payload) as resp:
                return resp.status in (200, 404)
        except Exception:
            return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._homeserver}/_matrix/client/v3/account/whoami"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
