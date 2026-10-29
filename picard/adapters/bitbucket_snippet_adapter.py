"""Bitbucket snippet adapter -- stores chunks as Bitbucket snippets.

Each write_chunk creates a private snippet via POST /snippets/{workspace}
with the encoded chunk as file content. Cold reads fetch via GET on the
snippet's self URL.

Requires: aiohttp, a Bitbucket app password and workspace slug.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "bitbucket-snippet"
_BASE_URL = "https://api.bitbucket.org/2.0"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class BitbucketSnippetAdapter(Adapter):

    def __init__(self, *, username: str = "", app_password: str = "",
                 workspace: str = "") -> None:
        self._username = username
        self._app_password = app_password
        self._workspace = workspace
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = None
            if self._username and self._app_password:
                auth = aiohttp.BasicAuth(self._username, self._app_password)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,bitbucket-app-password", "substrate": "bitbucket-snippet"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiohttp
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        filename = f"picard-{token}.txt"
        title = f"picard-{manifest_id[:16]}-{token}"
        data = aiohttp.FormData()
        data.add_field("title", title)
        data.add_field("is_private", "true")
        data.add_field("file", encoded_chunk,
                       filename=filename, content_type="text/plain")
        async with session.post(
                f"{_BASE_URL}/snippets/{self._workspace}",
                data=data) as resp:
            resp.raise_for_status()
            body = await resp.json()
        snippet_id = body["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "snippet_id": snippet_id,
                                  "filename": filename,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        url = (f"{_BASE_URL}/snippets/{self._workspace}"
               f"/{loc['snippet_id']}/files/{loc['filename']}")
        async with session.get(url) as resp:
            resp.raise_for_status()
            chunk = await resp.text()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/snippets/{self._workspace}"
                f"/{loc['snippet_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._workspace:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        url = f"{_BASE_URL}/snippets/{self._workspace}?role=owner"
        while url:
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for snippet in data.get("values", []):
                title = snippet.get("title", "")
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "snippet_id": snippet["id"],
                                              "filename": f"picard-{token}.txt",
                                              "manifest": manifest_id}),
                    ))
            url = (data.get("next") or "")
        return refs

    async def health_check(self) -> bool:
        if not self._username or not self._app_password or not self._workspace:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/user") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
