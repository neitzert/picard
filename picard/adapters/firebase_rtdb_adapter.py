"""Firebase Realtime Database adapter -- stores chunks as RTDB nodes.

Each write_chunk PUTs the encoded chunk at /picard/{token} in the RTDB.
Cold reads GET the same path.

Requires: aiohttp, a Firebase RTDB URL and an ID token or legacy database secret.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "firebase-rtdb"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class FirebaseRtdbAdapter(Adapter):

    def __init__(self, *, database_url: str = "",
                 auth_token: str = "") -> None:
        self._database_url = database_url.rstrip("/")
        self._auth_token = auth_token
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _url(self, path: str) -> str:
        url = f"{self._database_url}/{path}.json"
        if self._auth_token:
            url += f"?auth={self._auth_token}"
        return url

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=500,
            tags={"requires": "aiohttp,firebase-rtdb-url", "substrate": "firebase-rtdb-node"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {"chunk": encoded_chunk, "manifest": manifest_id}
        async with session.put(
                self._url(f"picard/{token}"),
                json=payload) as resp:
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
        session = await self._get_session()
        async with session.get(
                self._url(f"picard/{loc['token']}")) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                self._url(f"picard/{loc['token']}")) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        async with session.get(self._url("picard")) as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        if data:
            for token, node in data.items():
                if isinstance(node, dict) and node.get("manifest") == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "manifest": manifest_id}),
                    ))
        return refs

    async def health_check(self) -> bool:
        if not self._database_url:
            return False
        try:
            session = await self._get_session()
            async with session.get(self._url(".info/connected")) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
