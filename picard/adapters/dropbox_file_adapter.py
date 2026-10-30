"""Dropbox file adapter -- stores chunks as Dropbox files.

Each write_chunk uploads the encoded chunk as a text file at
/picard/{token}.txt. Cold reads download via /files/download.

Requires: aiohttp, a Dropbox access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "dropbox-file"
_CONTENT_URL = "https://content.dropboxapi.com/2"
_API_URL = "https://api.dropboxapi.com/2"


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
        self._access_token = access_token
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._access_token:
                headers["Authorization"] = f"Bearer {self._access_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=150_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,dropbox-access-token", "substrate": "dropbox-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        if not manifest_id:
            manifest_id = "unsealed"
        session = await self._get_session()
        path = f"/picard/{manifest_id[:16]}/{token}.txt"
        arg = json.dumps({"path": path, "mode": "overwrite"})
        async with session.post(
                f"{_CONTENT_URL}/files/upload",
                data=encoded_chunk.encode(),
                headers={"Content-Type": "application/octet-stream",
                         "Dropbox-API-Arg": arg}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "path": path,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        arg = json.dumps({"path": loc["path"]})
        async with session.post(
                f"{_CONTENT_URL}/files/download",
                headers={"Content-Type": "text/plain",
                         "Dropbox-API-Arg": arg}) as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.post(
                f"{_API_URL}/files/delete_v2",
                json={"path": loc["path"]}) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token:
            return []
        session = await self._get_session()
        folder = f"/picard/{manifest_id[:16]}"
        refs = []
        cursor = None
        has_more = True
        while has_more:
            if cursor:
                resp_data = {"cursor": cursor}
                url = f"{_API_URL}/files/list_folder/continue"
            else:
                resp_data = {"path": folder, "limit": 2000}
                url = f"{_API_URL}/files/list_folder"
            async with session.post(url, json=resp_data) as resp:
                if resp.status == 409:
                    break
                if resp.status != 200:
                    break
                data = await resp.json()
            for entry in data.get("entries", []):
                name = entry.get("name", "")
                if name.endswith(".txt"):
                    token = name[:-4]
                    path = entry.get("path_lower", f"/picard/{manifest_id[:16]}/{token}.txt")
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "path": path,
                                              "manifest": manifest_id}),
                    ))
            has_more = data.get("has_more", False)
            cursor = data.get("cursor")
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.post(
                    f"{_API_URL}/users/get_current_account",
                    data="null",
                    headers={"Content-Type": "application/json"}) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
