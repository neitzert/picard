"""OneDrive file adapter -- stores chunks as OneDrive files via Microsoft Graph API.

Each write_chunk uploads the encoded chunk as a text file at
/me/drive/special/approot:/picard/{token}.txt. Cold reads download the file.

Requires: aiohttp, a Microsoft Graph OAuth2 access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "onedrive-file"
_BASE_URL = "https://graph.microsoft.com/v1.0"


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
            max_chunk_bytes=4_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,microsoft-graph-token", "substrate": "onedrive-file-content"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        path = f"/me/drive/special/approot:/picard/{manifest_id[:16]}/{token}.txt:/content"
        async with session.put(
                f"{_BASE_URL}{path}",
                data=encoded_chunk.encode(),
                headers={"Content-Type": "text/plain"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        item_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "item_id": item_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/me/drive/items/{loc['item_id']}/content",
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
                f"{_BASE_URL}/me/drive/items/{loc['item_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token:
            return []
        session = await self._get_session()
        folder_path = f"/me/drive/special/approot:/picard/{manifest_id[:16]}:/children"
        refs = []
        url = f"{_BASE_URL}{folder_path}"
        while url:
            async with session.get(url) as resp:
                if resp.status in (404, 400):
                    break
                if resp.status != 200:
                    break
                data = await resp.json()
            for item in data.get("value", []):
                name = item.get("name", "")
                if name.endswith(".txt"):
                    token = name[:-4]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "item_id": item["id"],
                                              "manifest": manifest_id}),
                    ))
            url = data.get("@odata.nextLink")
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
