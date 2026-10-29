"""Intercom note adapter -- stores chunks as Intercom contact notes.

Each write_chunk creates a contact named picard-{token} then attaches a note
with the encoded chunk as the body. Cold reads fetch the note via GET /notes/{id}.

Requires: aiohttp, an Intercom access token.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "intercom-note"
_BASE_URL = "https://api.intercom.io"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class IntercomNoteAdapter(Adapter):

    def __init__(self, *, access_token: str = "") -> None:
        self._access_token = access_token
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {
                "Accept": "application/json",
                "Intercom-Version": "2.10",
            }
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
            tags={"requires": "aiohttp,intercom-access-token", "substrate": "intercom-contact-note"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        contact_resp = await session.post(
            f"{_BASE_URL}/contacts",
            json={"role": "user", "name": f"picard-{token}",
                  "external_id": token},
        )
        contact_resp.raise_for_status()
        contact = await contact_resp.json()
        contact_id = contact["id"]
        note_resp = await session.post(
            f"{_BASE_URL}/notes",
            json={"contact_id": contact_id, "body": encoded_chunk},
        )
        note_resp.raise_for_status()
        note = await note_resp.json()
        note_id = note["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "note_id": note_id,
                                  "contact_id": contact_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/notes/{loc['note_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["body"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{_BASE_URL}/contacts/{loc['contact_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "note_id": "",
                                          "contact_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._access_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
