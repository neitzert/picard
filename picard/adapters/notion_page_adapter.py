"""Notion page adapter -- stores chunks as Notion page content blocks.

Each write_chunk creates a private Notion page under a parent page ID
with the encoded chunk as a paragraph block.

Requires: aiohttp, Notion integration token, parent_page_id.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "notion-page"
_BASE = "https://api.notion.com/v1"
_VERSION = "2022-06-28"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class NotionPageAdapter(Adapter):

    def __init__(self, *, token: str = "", parent_page_id: str = "") -> None:
        self._token = token
        self._parent_page_id = parent_page_id
        self._session = None
        self._reg: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _VERSION,
            "Content-Type": "application/json",
        }

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=2000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
            tags={"requires": "notion-token,parent-page-id",
                  "substrate": "notion-page"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        payload = {
            "parent": {"page_id": self._parent_page_id},
            "properties": {"title": {"title": [{"text": {"content": f"picard:{token}"}}]}},
            "children": [{"object": "block", "type": "paragraph",
                          "paragraph": {"rich_text": [{"text": {"content": encoded_chunk}}]}}],
        }
        async with session.post(f"{_BASE}/pages", json=payload,
                                headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        page_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "page_id": page_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(f"{_BASE}/blocks/{loc['page_id']}/children",
                               headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for block in data.get("results", []):
            if block.get("type") == "paragraph":
                texts = block["paragraph"]["rich_text"]
                if texts:
                    chunk = texts[0]["text"]["content"]
                    self._reg[loc["token"]] = chunk
                    return chunk
        raise KeyError(f"notion-page chunk not found: {loc['token']}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.patch(f"{_BASE}/pages/{loc['page_id']}",
                                  json={"archived": True},
                                  headers=self._headers()) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t, "page_id": "",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._token or not self._parent_page_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE}/users/me",
                                    headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
