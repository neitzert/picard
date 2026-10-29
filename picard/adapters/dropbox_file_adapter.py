"""Dropbox file adapter -- stores chunks as Dropbox files.

Each write_chunk uploads the encoded chunk as a file to
/picard/{manifest}/{token} via the Dropbox API v2.

Requires: aiohttp, Dropbox access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "dropbox-file"
_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"
_LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class DropboxFileAdapter(Adapter):

    def __init__(self, *, access_token: str = "") -> None:
        self._token = access_token
        self._session = None
        self._reg: dict[str, str] = {}

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "dropbox-access-token",
                  "substrate": "dropbox-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        path = f"/picard/{manifest_id}/{token}"
        session = await self._get_session()
        headers = {
            **self._auth_header(),
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "add"}),
        }
        async with session.post(_UPLOAD_URL, headers=headers,
                                data=encoded_chunk.encode()) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "path": path,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        headers = {
            **self._auth_header(),
            "Dropbox-API-Arg": json.dumps({"path": loc["path"]}),
        }
        async with session.post(_DOWNLOAD_URL, headers=headers) as resp:
            resp.raise_for_status()
            chunk = (await resp.read()).decode()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        headers = {**self._auth_header(), "Content-Type": "application/json"}
        async with session.post(_DELETE_URL, headers=headers,
                                json={"path": loc["path"]}) as resp:
            return resp.status in (200, 409)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps(
                                   {"token": t,
                                    "path": f"/picard/{manifest_id}/{t}",
                                    "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._token:
            return False
        try:
            session = await self._get_session()
            headers = {**self._auth_header(), "Content-Type": "application/json"}
            async with session.post(
                    "https://api.dropboxapi.com/2/users/get_current_account",
                    headers=headers, data=b"null") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
