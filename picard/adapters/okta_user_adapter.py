"""Okta user adapter -- stores chunks as Okta user profile custom attributes.

Each write_chunk creates an Okta user with a picard_ prefixed login and
stores the encoded chunk in a custom profile attribute.

Requires: aiohttp, Okta org URL, SSWS API token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "okta-user"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class OktaUserAdapter(Adapter):

    def __init__(self, *, org_url: str = "", api_token: str = "") -> None:
        self._base = org_url.rstrip("/")
        self._token = api_token
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"SSWS {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "okta-org-url,ssws-token",
                  "substrate": "okta-user-profile"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "profile": {
                "firstName": "picard",
                "lastName": token,
                "email": f"picard_{token}@picard.invalid",
                "login": f"picard_{token}@picard.invalid",
                "picard_chunk": encoded_chunk,
                "picard_manifest": manifest_id,
            }
        }
        async with session.post(f"{self._base}/api/v1/users",
                                params={"activate": "false"},
                                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        user_id = data["id"]
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
                f"{self._base}/api/v1/users/{loc['user_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["profile"].get("picard_chunk", "")
        if not chunk:
            raise KeyError(f"okta-user chunk not found: {loc['token']}")
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        # Must deactivate before deleting
        await session.post(
            f"{self._base}/api/v1/users/{loc['user_id']}/lifecycle/deactivate")
        async with session.delete(
                f"{self._base}/api/v1/users/{loc['user_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._base or not self._token:
            return []
        import urllib.parse
        session = await self._get_session()
        q = urllib.parse.quote(f'profile.picard_manifest eq "{manifest_id}"')
        refs = []
        url = f"{self._base}/api/v1/users?search={q}&limit=200"
        while url:
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                users = await resp.json()
                link = resp.headers.get("Link", "")
            for user in users:
                login = (user.get("profile") or {}).get("login", "")
                token = login.removeprefix("picard_").removesuffix("@picard.invalid")
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "user_id": user["id"],
                                          "manifest": manifest_id}),
                ))
            url = ""
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
        return refs

    async def health_check(self) -> bool:
        if not self._base or not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base}/api/v1/users/me") as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
