"""Zendesk ticket adapter -- stores chunks as Zendesk ticket comments.

Each write_chunk creates a ticket with the encoded chunk as the description.
Cold reads fetch via GET /api/v2/tickets/{id}.json.

Requires: aiohttp, a Zendesk subdomain, agent email, and API token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "zendesk-ticket"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ZendeskTicketAdapter(Adapter):

    def __init__(self, *, subdomain: str = "", email: str = "",
                 api_token: str = "") -> None:
        self._subdomain = subdomain
        self._email = email
        self._api_token = api_token
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        return f"https://{self._subdomain}.zendesk.com/api/v2"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(f"{self._email}/token", self._api_token)
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,zendesk-credentials", "substrate": "zendesk-ticket-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "ticket": {
                "subject": f"picard-{token}",
                "comment": {"body": encoded_chunk},
                "tags": ["picard", f"manifest-{manifest_id[:16]}"],
            }
        }
        async with session.post(
                f"{self._base_url()}/tickets.json",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        ticket_id = data["ticket"]["id"]
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
                f"{self._base_url()}/tickets/{loc['ticket_id']}.json") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["ticket"]["description"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url()}/tickets/{loc['ticket_id']}.json") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._subdomain or not self._email or not self._api_token:
            return []
        import urllib.parse
        session = await self._get_session()
        tag = f"manifest-{manifest_id[:16]}"
        query = urllib.parse.quote(f"type:ticket tags:{tag}")
        refs = []
        url = f"{self._base_url()}/search.json?query={query}&per_page=100"
        while url:
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for ticket in data.get("results", []):
                subject = ticket.get("subject", "")
                token = subject.removeprefix("picard-")
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "ticket_id": ticket["id"],
                                          "manifest": manifest_id}),
                ))
            url = data.get("next_page") or ""
        return refs

    async def health_check(self) -> bool:
        if not self._subdomain or not self._email or not self._api_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base_url()}/users/me.json") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
