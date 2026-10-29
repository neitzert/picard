"""HackMD note adapter -- stores chunks as HackMD (HedgeDoc cloud) note content.

Each write_chunk creates a private note via POST /v1/notes with the encoded
chunk as the note body. Cold reads fetch via GET /v1/notes/{note_id}.

Requires: aiohttp, a HackMD API token (https://hackmd.io/settings#api).
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "hackmd-note"
_BASE_URL = "https://api.hackmd.io/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class HackmdNoteAdapter(Adapter):

    def __init__(self, *, api_token: str = "",
                 base_url: str = _BASE_URL) -> None:
        self._api_token = api_token
        self._base_url = base_url.rstrip("/")
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._api_token:
                headers["Authorization"] = f"Bearer {self._api_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            tags={"requires": "aiohttp,hackmd-token", "substrate": "hackmd-note"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "title": f"picard-{token}",
            "content": encoded_chunk,
            "readPermission": "owner",
            "writePermission": "owner",
        }
        async with session.post(f"{self._base_url}/notes",
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
                f"{self._base_url}/notes/{loc['note_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["content"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._base_url}/notes/{loc['note_id']}") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "note_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base_url}/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
