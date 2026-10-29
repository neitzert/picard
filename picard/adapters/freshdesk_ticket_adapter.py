"""Freshdesk ticket adapter -- stores chunks as Freshdesk ticket descriptions.

Each write_chunk creates a ticket with the encoded chunk as the description.
Cold reads fetch via GET /api/v2/tickets/{id}.

Requires: aiohttp, a Freshdesk domain (subdomain.freshdesk.com) and API key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "freshdesk-ticket"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class FreshdeskTicketAdapter(Adapter):

    def __init__(self, *, domain: str = "", api_key: str = "") -> None:
        self._domain = domain
        self._api_key = api_key
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        return f"https://{self._domain}.freshdesk.com/api/v2"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(self._api_key, "X")
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,freshdesk-api-key", "substrate": "freshdesk-ticket-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "subject": f"picard-{token}",
            "description": encoded_chunk,
            "email": "picard@example.com",
            "priority": 1,
            "status": 2,
            "tags": [f"picard", f"manifest-{manifest_id[:8]}"],
        }
        async with session.post(
                f"{self._base_url()}/tickets",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        ticket_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "ticket_id": ticket_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._base_url()}/tickets/{loc['ticket_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["description_text"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url()}/tickets/{loc['ticket_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "ticket_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._domain or not self._api_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base_url()}/agents/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
