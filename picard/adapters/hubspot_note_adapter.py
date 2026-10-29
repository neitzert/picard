"""HubSpot engagement note adapter -- stores chunks as HubSpot notes.

Each write_chunk creates an engagement note via POST /engagements/v1/engagements.
Cold reads fetch via GET /engagements/v1/engagements/{id}.

Requires: aiohttp, a HubSpot private app access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "hubspot-note"
_BASE_URL = "https://api.hubapi.com"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class HubspotNoteAdapter(Adapter):

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
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,hubspot-access-token", "substrate": "hubspot-engagement-note"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "metadata": {"body": encoded_chunk},
            "associations": {},
        }
        async with session.post(
                f"{_BASE_URL}/engagements/v1/engagements",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        eng_id = data["engagement"]["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "eng_id": eng_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/engagements/v1/engagements/{loc['eng_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["metadata"]["body"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/engagements/v1/engagements/{loc['eng_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "eng_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE_URL}/crm/v3/objects/contacts?limit=1") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
