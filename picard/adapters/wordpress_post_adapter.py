"""WordPress REST API adapter -- stores chunks as WordPress post content.

Each write_chunk creates a private draft post with the encoded chunk as
the content field. Cold reads fetch via GET /wp-json/wp/v2/posts/{id}.

Requires: aiohttp, a WordPress site URL, username, and application password.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "wordpress-post"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class WordpressPostAdapter(Adapter):

    def __init__(self, *, site_url: str = "", username: str = "",
                 app_password: str = "") -> None:
        self._site_url = site_url.rstrip("/")
        self._username = username
        self._app_password = app_password
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
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
            tags={"requires": "aiohttp,wordpress-app-password", "substrate": "wordpress-post-content"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard-{token}",
            "content": encoded_chunk,
            "status": "private",
        }
        async with session.post(
                f"{self._site_url}/wp-json/wp/v2/posts",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        post_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "post_id": post_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._site_url}/wp-json/wp/v2/posts/{loc['post_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["content"]["raw"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._site_url}/wp-json/wp/v2/posts/{loc['post_id']}",
                params={"force": "true"}) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "post_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._site_url or not self._username or not self._app_password:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._site_url}/wp-json/wp/v2/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
