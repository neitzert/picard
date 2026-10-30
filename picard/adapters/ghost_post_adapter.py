"""Ghost post adapter -- stores chunks as Ghost CMS posts.

Each write_chunk creates a draft post with the encoded chunk as HTML content
via the Ghost Admin API.

Requires: aiohttp, Ghost Admin API URL, Staff Access Token (id:secret format).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ghost-post"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _make_jwt(key_id: str, secret_hex: str) -> str:
    import base64
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "kid": key_id, "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": now, "exp": now + 300,
                    "aud": "/admin/"}).encode()
    ).rstrip(b"=").decode()
    signing_input = f"{header}.{payload}".encode()
    secret_bytes = bytes.fromhex(secret_hex)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


class GhostPostAdapter(Adapter):

    def __init__(self, *, admin_url: str = "",
                 admin_key: str = "") -> None:
        self._base = admin_url.rstrip("/")
        self._admin_key = admin_key
        self._session = None
        self._reg: dict[str, str] = {}

    def _jwt(self) -> str:
        if ":" not in self._admin_key:
            return self._admin_key
        key_id, secret = self._admin_key.split(":", 1)
        return _make_jwt(key_id, secret)

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Ghost {self._jwt()}",
            "Content-Type": "application/json",
        }

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "ghost-admin-key,admin-url",
                  "substrate": "ghost-post-html"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {"posts": [{
            "title": f"picard:{manifest_id[:16]}:{token}",
            "html": f"<p>{encoded_chunk}</p>",
            "status": "draft",
            "tags": [{"name": "picard"}],
        }]}
        async with session.post(
                f"{self._base}/ghost/api/admin/posts/",
                headers=self._headers(), json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        post_id = data["posts"][0]["id"]
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
                f"{self._base}/ghost/api/admin/posts/{loc['post_id']}/",
                headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        import re
        chunk = re.sub(r"<[^>]+>", "",
                       data["posts"][0].get("html", "")).strip()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/ghost/api/admin/posts/{loc['post_id']}/",
                headers=self._headers()) as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._base or not self._admin_key:
            return []
        session = await self._get_session()
        prefix = f"picard:{manifest_id[:16]}:"
        refs = []
        page = 1
        while True:
            async with session.get(
                    f"{self._base}/ghost/api/admin/posts/",
                    headers=self._headers(),
                    params={"filter": f"title:~'{prefix}'",
                            "limit": "100", "page": str(page),
                            "fields": "id,title"}) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            posts = data.get("posts", [])
            for post in posts:
                title = post.get("title", "")
                if title.startswith(prefix):
                    token = title[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "post_id": post["id"],
                                              "manifest": manifest_id}),
                    ))
            meta = data.get("meta", {}).get("pagination", {})
            if page >= meta.get("pages", 1):
                break
            page += 1
        return refs

    async def health_check(self) -> bool:
        if not self._base or not self._admin_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base}/ghost/api/admin/site/",
                    headers=self._headers()) as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
