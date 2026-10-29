"""HubSpot note adapter -- stores chunks as HubSpot CRM notes.

Each write_chunk creates a CRM note via POST /crm/v3/objects/notes.
Cold reads fetch via GET /crm/v3/objects/notes/{id}.
list_chunks searches by body content prefix using the CRM search API.

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
        body = f"picard-{manifest_id[:16]}-{token}\n{encoded_chunk}"
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(__import__("time").time() * 1000)),
            }
        }
        async with session.post(
                f"{_BASE_URL}/crm/v3/objects/notes",
                json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        note_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "note_id": note_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/crm/v3/objects/notes/{loc['note_id']}",
                params={"properties": "hs_note_body"}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        body = data["properties"]["hs_note_body"]
        chunk = body.split("\n", 1)[1] if "\n" in body else body
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/crm/v3/objects/notes/{loc['note_id']}") as resp:
            return resp.status in (204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._access_token:
            return []
        session = await self._get_session()
        prefix = f"picard-{manifest_id[:16]}-"
        refs = []
        after = None
        while True:
            body: dict = {
                "filterGroups": [{"filters": [{
                    "propertyName": "hs_note_body",
                    "operator": "CONTAINS_TOKEN",
                    "value": prefix,
                }]}],
                "properties": ["hs_note_body"],
                "limit": 100,
            }
            if after:
                body["after"] = after
            async with session.post(
                    f"{_BASE_URL}/crm/v3/objects/notes/search",
                    json=body) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for result in data.get("results", []):
                note_body = (result.get("properties") or {}).get("hs_note_body", "")
                first_line = note_body.split("\n", 1)[0]
                if first_line.startswith(prefix):
                    token = first_line[len(prefix):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token,
                                              "note_id": result["id"],
                                              "manifest": manifest_id}),
                    ))
            paging = data.get("paging", {})
            after = (paging.get("next") or {}).get("after")
            if not after:
                break
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE_URL}/crm/v3/objects/notes?limit=1") as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
