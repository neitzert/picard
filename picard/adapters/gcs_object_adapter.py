"""Google Cloud Storage object adapter -- stores chunks as GCS objects.

Each write_chunk uploads the encoded chunk as a text object. Cold reads
download via the GCS XML API or JSON API with resumable uploads disabled.

Requires: aiohttp, a GCS bucket name and an OAuth2 access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "gcs-object"
_UPLOAD_URL = "https://storage.googleapis.com/upload/storage/v1/b"
_DOWNLOAD_URL = "https://storage.googleapis.com/storage/v1/b"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GcsObjectAdapter(Adapter):

    def __init__(self, *, bucket: str = "", access_token: str = "") -> None:
        self._bucket = bucket
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
            max_chunk_bytes=5_000_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "aiohttp,gcs-oauth-token", "substrate": "gcs-object-data"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        obj_name = f"picard/{token}"
        import urllib.parse
        url = (f"{_UPLOAD_URL}/{urllib.parse.quote(self._bucket, safe='')}"
               f"/o?uploadType=media&name={urllib.parse.quote(obj_name, safe='')}")
        async with session.post(
                url,
                data=encoded_chunk.encode(),
                headers={"Content-Type": "text/plain"}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "obj_name": obj_name,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        import urllib.parse
        url = (f"{_DOWNLOAD_URL}/{urllib.parse.quote(self._bucket, safe='')}"
               f"/o/{urllib.parse.quote(loc['obj_name'], safe='')}?alt=media")
        async with session.get(url) as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        import urllib.parse
        url = (f"{_DOWNLOAD_URL}/{urllib.parse.quote(self._bucket, safe='')}"
               f"/o/{urllib.parse.quote(loc['obj_name'], safe='')}")
        async with session.delete(url) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "obj_name": f"picard/{token}",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._bucket or not self._access_token:
            return False
        try:
            session = await self._get_session()
            import urllib.parse
            url = (f"{_DOWNLOAD_URL}/{urllib.parse.quote(self._bucket, safe='')}"
                   f"/o?maxResults=1")
            async with session.get(url) as resp:
                return resp.status in (200, 404)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
