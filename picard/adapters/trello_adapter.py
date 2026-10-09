"""Trello adapter — stores chunks as card descriptions.

Each chunk becomes a Trello card in the configured list, with the
encoded chunk data in the card description field.

Requires: pip install picard[trello]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.trello.com/1"


class TrelloAdapter(Adapter):
    """Scatter chunks into Trello card descriptions."""

    def __init__(
        self,
        api_key: str,
        token: str,
        board_id: str,
        list_id: str,
        *,
        max_chunk_bytes: int = 16384,
    ) -> None:
        self._api_key = api_key
        self._token = token
        self._board_id = board_id
        self._list_id = list_id
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession()
        return self._client

    def _auth_params(self) -> dict:
        return {"key": self._api_key, "token": self._token}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="trello",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=100,
        )

    def _card_name(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"pchk:{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except ValueError:
            return f"pchk:{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        name = self._card_name(encoded_chunk)
        params = {**self._auth_params(), "idList": self._list_id, "name": name, "desc": encoded_chunk}
        async with client.post(f"{_API}/cards", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        card_id = data["id"]
        loc = f"trello://{self._board_id}/{card_id}"
        return StoredChunkRef(adapter_name="trello", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        card_id = ref.location.rsplit("/", 1)[-1]
        async with client.get(
            f"{_API}/cards/{card_id}",
            params={**self._auth_params(), "fields": "desc"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["desc"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        card_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with client.delete(
                f"{_API}/cards/{card_id}",
                params=self._auth_params(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"pchk:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        async with client.get(
            f"{_API}/lists/{self._list_id}/cards",
            params={**self._auth_params(), "fields": "name"},
        ) as resp:
            if resp.status != 200:
                return []
            cards = await resp.json()
        for card in cards:
            if card.get("name", "").startswith(prefix):
                loc = f"trello://{self._board_id}/{card['id']}"
                refs.append(StoredChunkRef(adapter_name="trello", location=loc))
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.get(
                f"{_API}/members/me",
                params=self._auth_params(),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
