"""OneDrive file adapter -- stores chunks as files in Microsoft OneDrive.

Each write_chunk PUTs the encoded chunk to
/me/drive/root:/picard/{manifest}/{token}:/content via the Microsoft Graph API.

Requires: aiohttp, Microsoft Graph OAuth2 Bearer token with Files.ReadWrite scope.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "onedrive-file"
_BASE = "https://graph.microsoft.com/v1.0"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class OnedriveFileAdapter(Adapter):

    def __init__(self, *, access_token: str = "") -> None:
        self._token = access_token
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _item_url(self, manifest_id: str, token: str) -> str:
        return f"{_BASE}/me/drive/root:/picard/{manifest_id}/{token}:/content"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "microsoft-graph-token,Files.ReadWrite",
                  "substrate": "onedrive-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        headers = {**self._headers(), "Content-Type": "text/plain"}
        async with session.put(self._item_url(manifest_id, token),
                               headers=headers,
                               data=encoded_chunk.encode()) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(self._item_url(loc["manifest"], loc["token"]),
                               headers=self._headers()) as resp:
            resp.raise_for_status()
            chunk = (await resp.read()).decode()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        url = (f"{_BASE}/me/drive/root:/picard"
               f"/{loc['manifest']}/{loc['token']}")
        session = await self._get_session()
        async with session.delete(url, headers=self._headers()) as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t,
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE}/me",
                                    headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
