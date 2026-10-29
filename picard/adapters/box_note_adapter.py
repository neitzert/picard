"""Box note adapter -- stores chunks as Box files.

Each write_chunk uploads the encoded chunk as a text file via the
Box Upload API. Cold reads download via GET /files/{id}/content.

Requires: aiohttp, a Box developer token or OAuth2 access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "box-note"
_API_URL = "https://api.box.com/2.0"
_UPLOAD_URL = "https://upload.box.com/api/2.0"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class BoxNoteAdapter(Adapter):

    def __init__(self, *, access_token: str = "",
                 folder_id: str = "0") -> None:
        self._access_token = access_token
        self._folder_id = folder_id
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
            max_chunk_bytes=5_000_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,box-access-token", "substrate": "box-file-content"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp as _aio
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        filename = f"picard-{manifest_id[:16]}-{token}.txt"
        attributes = json.dumps({
            "name": filename,
            "parent": {"id": self._folder_id},
        })
        form = _aio.FormData()
        form.add_field("attributes", attributes, content_type="application/json")
        form.add_field("file", encoded_chunk.encode(),
                       filename=filename,
                       content_type="text/plain")
        async with session.post(
                f"{_UPLOAD_URL}/files/content", data=form) as resp:
            resp.raise_for_status()
            data = await resp.json()
        file_id = data["entries"][0]["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "file_id": file_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_API_URL}/files/{loc['file_id']}/content",
                allow_redirects=True) as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_API_URL}/files/{loc['file_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        marker = None
        while True:
            params: dict = {"limit": "1000", "fields": "id,name"}
            if marker:
                params["marker"] = marker
            async with session.get(
                    f"{_API_URL}/folders/{self._folder_id}/items",
                    params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for item in data.get("entries", []):
                name = item.get("name", "")
                if name.startswith(prefix) and name.endswith(".txt"):
                    token = name[len(prefix):-4]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "file_id": item["id"],
                                              "manifest": manifest_id}),
                    ))
            marker = data.get("next_marker")
            if not marker:
                break
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_API_URL}/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
