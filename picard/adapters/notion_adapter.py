"""Notion adapter — stores chunks as database rows.

Each chunk becomes a page in a Notion database with the encoded data
in a rich_text property and a chunk key in the title.

Requires: pip install picard[notion]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.notion.com/v1"


class NotionAdapter(Adapter):
    """Scatter chunks into Notion database rows."""

    def __init__(
        self,
        api_key: str,
        database_id: str,
        *,
        max_chunk_bytes: int = 2000,
    ) -> None:
        self._api_key = api_key
        self._db_id = database_id
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="notion",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=90,
        )

    def _chunk_title(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"pchk:{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except ValueError:
            return f"pchk:{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        title = self._chunk_title(encoded_chunk)
        payload = {
            "parent": {"database_id": self._db_id},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Data": {"rich_text": [{"text": {"content": encoded_chunk}}]},
            },
        }
        async with client.post(f"{_API}/pages", json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        page_id = data["id"]
        loc = f"notion://{self._db_id}/{page_id}"
        return StoredChunkRef(adapter_name="notion", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        page_id = ref.location.rsplit("/", 1)[-1]
        async with client.get(f"{_API}/pages/{page_id}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        texts = data["properties"]["Data"]["rich_text"]
        if not texts:
            raise KeyError(f"chunk not found: {page_id}")
        return texts[0]["text"]["content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        page_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with client.patch(
                f"{_API}/pages/{page_id}",
                json={"archived": True},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        payload = {
            "filter": {
                "property": "Name",
                "title": {"starts_with": f"pchk:{manifest_id}:"},
            }
        }
        refs: list[StoredChunkRef] = []
        try:
            async with client.post(
                f"{_API}/databases/{self._db_id}/query",
                json=payload,
            ) as resp:
                data = await resp.json()
            for page in data.get("results", []):
                loc = f"notion://{self._db_id}/{page['id']}"
                refs.append(StoredChunkRef(adapter_name="notion", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.get(f"{_API}/users/me") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
