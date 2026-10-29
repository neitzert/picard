"""Auth0 user adapter -- stores chunks in Auth0 user_metadata via Management API.

Each write_chunk creates an Auth0 user with picard_{token} as username
and stores the encoded chunk in user_metadata.picard_chunk.

Requires: aiohttp, Auth0 domain, Management API v2 Bearer token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "auth0-user"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class Auth0UserAdapter(Adapter):

    def __init__(self, *, domain: str = "", management_token: str = "") -> None:
        self._base = f"https://{domain}" if domain and not domain.startswith("http") else domain
        self._base = self._base.rstrip("/")
        self._token = management_token
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=16384,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "auth0-domain,management-api-token",
                  "substrate": "auth0-user-metadata"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "connection": "Username-Password-Authentication",
            "email": f"picard_{token}@picard.invalid",
            "password": f"Picard!{token[:8]}X9",
            "user_metadata": {"picard_chunk": encoded_chunk,
                               "picard_manifest": manifest_id},
        }
        async with session.post(f"{self._base}/api/v2/users",
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        user_id = data["user_id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "user_id": user_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/api/v2/users/{loc['user_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data.get("user_metadata", {}).get("picard_chunk", "")
        if not chunk:
            raise KeyError(f"auth0-user chunk not found: {loc['token']}")
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/api/v2/users/{loc['user_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t, "user_id": "",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._base or not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base}/api/v2/users",
                                    params={"per_page": "1"}) as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
