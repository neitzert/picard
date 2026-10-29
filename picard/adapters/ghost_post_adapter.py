"""Ghost CMS adapter -- stores chunks as Ghost draft post content.

Each write_chunk creates a draft post with the encoded chunk as the
HTML content. Cold reads fetch via GET /ghost/api/admin/posts/{id}.

Requires: aiohttp, a Ghost Admin API key (ID:Secret format) and site URL.
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


def _ghost_jwt(api_key: str) -> str:
    key_id, secret = api_key.split(":")
    iat = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": key_id}
    payload = {"iat": iat, "exp": iat + 300, "aud": "/admin/"}
    import base64 as _b64
    def _b64url(data: bytes) -> str:
        return _b64.urlsafe_b64encode(data).rstrip(b"=").decode()
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    sig_input = f"{h}.{p}".encode()
    sig = hmac.new(bytes.fromhex(secret), sig_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


class GhostPostAdapter(Adapter):

    def __init__(self, *, site_url: str = "", admin_api_key: str = "") -> None:
        self._site_url = site_url.rstrip("/")
        self._admin_api_key = admin_api_key
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _auth_header(self) -> str:
        return f"Ghost {_ghost_jwt(self._admin_api_key)}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,ghost-admin-api-key", "substrate": "ghost-draft-post-html"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        url = f"{self._site_url}/ghost/api/admin/posts/"
        payload = {
            "posts": [{
                "title": f"picard-{token}",
                "html": encoded_chunk,
                "status": "draft",
                "visibility": "internal",
            }]
        }
        async with session.post(
                url, json=payload,
                headers={"Authorization": self._auth_header()}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        post_id = data["posts"][0]["id"]
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
        url = f"{self._site_url}/ghost/api/admin/posts/{loc['post_id']}"
        async with session.get(
                url, headers={"Authorization": self._auth_header()}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["posts"][0]["html"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        url = f"{self._site_url}/ghost/api/admin/posts/{loc['post_id']}"
        async with session.delete(
                url, headers={"Authorization": self._auth_header()}) as resp:
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
        if not self._site_url or not self._admin_api_key or ":" not in self._admin_api_key:
            return False
        try:
            session = await self._get_session()
            url = f"{self._site_url}/ghost/api/admin/site/"
            async with session.get(
                    url, headers={"Authorization": self._auth_header()}) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
