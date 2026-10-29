"""Trello card adapter -- stores chunks as Trello card descriptions.

Each write_chunk creates a Trello card on a configured list with the
encoded chunk in the card description.

Requires: aiohttp, Trello API key, token, list_id.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "trello-card"
_BASE = "https://api.trello.com/1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class TrelloCardAdapter(Adapter):

    def __init__(self, *, api_key: str = "", api_token: str = "",
                 list_id: str = "") -> None:
        self._api_key = api_key
        self._api_token = api_token
        self._list_id = list_id
        self._session = None
        self._reg: dict[str, str] = {}

    def _auth(self) -> dict[str, str]:
        return {"key": self._api_key, "token": self._api_token}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=16384,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "trello-api-key,list-id",
                  "substrate": "trello-card-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        params = {**self._auth(), "idList": self._list_id,
                  "name": f"picard:{manifest_id[:16]}:{token}",
                  "desc": encoded_chunk}
        async with session.post(f"{_BASE}/cards", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        card_id = data["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "card_id": card_id,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(f"{_BASE}/cards/{loc['card_id']}",
                               params=self._auth()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["desc"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(f"{_BASE}/cards/{loc['card_id']}",
                                   params=self._auth()) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._api_key or not self._list_id:
            return []
        session = await self._get_session()
        prefix = f"picard:{manifest_id[:16]}:"
        async with session.get(f"{_BASE}/lists/{self._list_id}/cards",
                               params={**self._auth(), "fields": "id,name"}) as resp:
            if resp.status != 200:
                return []
            cards = await resp.json()
        refs = []
        for card in cards:
            name = card.get("name", "")
            if name.startswith(prefix):
                token = name[len(prefix):]
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "card_id": card["id"],
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._api_key or not self._api_token or not self._list_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE}/members/me",
                                    params=self._auth()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
