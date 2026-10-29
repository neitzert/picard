"""Contentful entry adapter -- stores chunks as Contentful content entries.

Each write_chunk creates a Contentful entry with the encoded chunk in a
'body' field. Cold reads fetch via GET /spaces/{sid}/entries/{eid}.

Requires: aiohttp, a Contentful Management API token, space ID, and
content type ID (with a 'body' Text field).
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "contentful-entry"
_BASE_URL = "https://api.contentful.com"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ContentfulEntryAdapter(Adapter):

    def __init__(self, *, mgmt_token: str = "", space_id: str = "",
                 content_type_id: str = "picardChunk",
                 locale: str = "en-US") -> None:
        self._mgmt_token = mgmt_token
        self._space_id = space_id
        self._content_type_id = content_type_id
        self._locale = locale
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {}
            if self._mgmt_token:
                headers["Authorization"] = f"Bearer {self._mgmt_token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def _entries_url(self) -> str:
        return f"{_BASE_URL}/spaces/{self._space_id}/entries"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=50000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=55,
            tags={"requires": "aiohttp,contentful-mgmt-token", "substrate": "contentful-entry-field"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "fields": {
                "title": {self._locale: f"picard-{token}"},
                "body": {self._locale: encoded_chunk},
                "manifest": {self._locale: manifest_id},
            }
        }
        headers = {
            "X-Contentful-Content-Type": self._content_type_id,
        }
        async with session.post(
                self._entries_url(), json=payload,
                headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
        entry_id = data["sys"]["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "entry_id": entry_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._entries_url()}/{loc['entry_id']}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["fields"]["body"][self._locale]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        entry_url = f"{self._entries_url()}/{loc['entry_id']}"
        async with session.delete(entry_url + "/published") as _:
            pass
        async with session.delete(entry_url) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "entry_id": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._mgmt_token or not self._space_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE_URL}/spaces/{self._space_id}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
