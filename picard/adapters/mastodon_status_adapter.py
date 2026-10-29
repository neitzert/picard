"""Mastodon status adapter -- stores chunks as private Mastodon toots.

Each write_chunk posts a private status to a Mastodon instance with the
encoded chunk as the status text.

Requires: aiohttp, Mastodon instance URL, access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mastodon-status"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MastodonStatusAdapter(Adapter):

    def __init__(self, *, instance_url: str = "",
                 access_token: str = "") -> None:
        self._base = instance_url.rstrip("/")
        self._access_token = access_token
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(headers={
                "Authorization": f"Bearer {self._access_token}",
            })
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=500,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
            tags={"requires": "mastodon-instance-url,access-token",
                  "substrate": "mastodon-status"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "status": encoded_chunk,
            "visibility": "direct",
            "spoiler_text": f"picard:{token}",
        }
        async with session.post(
                f"{self._base}/api/v1/statuses", data=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        status_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "status_id": status_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base}/api/v1/statuses/{loc['status_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        # Strip HTML tags from content
        import re
        chunk = re.sub(r"<[^>]+>", "", data.get("content", "")).strip()
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base}/api/v1/statuses/{loc['status_id']}") as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t, "status_id": "",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._base or not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base}/api/v1/accounts/verify_credentials") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
