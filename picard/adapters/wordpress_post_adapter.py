"""WordPress post adapter -- stores chunks as private WordPress posts.

Each write_chunk creates a private post with the encoded chunk as the
post content via the WordPress REST API v2.

Requires: aiohttp, WordPress site URL, username, application password.
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
        self._base = f"{site_url.rstrip('/')}/wp-json/wp/v2"
        self._username = username
        self._app_password = app_password
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp, base64
            creds = base64.b64encode(
                f"{self._username}:{self._app_password}".encode()
            ).decode()
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "wordpress-app-password",
                  "substrate": "wordpress-post-content"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard:{manifest_id[:16]}:{token}",
            "content": encoded_chunk,
            "status": "private",
        }
        async with session.post(f"{self._base}/posts", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        post_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "post_id": post_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/posts/{loc['post_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        import re
        chunk = re.sub(r"<[^>]+>", "", data["content"]["rendered"]).strip()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/posts/{loc['post_id']}",
                params={"force": "true"}) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._base:
            return []
        session = await self._get_session()
        prefix = f"picard:{manifest_id[:16]}:"
        refs = []
        page = 1
        while True:
            params = f"search={prefix}&status=private&per_page=100&page={page}"
            async with session.get(f"{self._base}/posts?{params}") as resp:
                if resp.status != 200:
                    break
                posts = await resp.json()
            if not posts:
                break
            for post in posts:
                import html as _html
                title = _html.unescape(
                    (post.get("title") or {}).get("rendered", ""))
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "post_id": post["id"],
                                              "manifest": manifest_id}),
                    ))
            if len(posts) < 100:
                break
            page += 1
        return refs

    async def health_check(self) -> bool:
        if not self._username or not self._app_password:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base}/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
