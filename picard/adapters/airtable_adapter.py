"""Airtable adapter — stores chunks as table records.

Each chunk becomes a record in an Airtable base/table with the
encoded data in a "Data" long-text field and a chunk key in "Key".

Requires: pip install picard-store[airtable]  (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_API = "https://api.airtable.com/v0"


class AirtableAdapter(Adapter):
    """Scatter chunks into Airtable records."""

    def __init__(
        self,
        api_key: str,
        base_id: str,
        *,
        table_name: str = "chunks",
        max_chunk_bytes: int = 100_000,
    ) -> None:
        self._api_key = api_key
        self._base_id = base_id
        self._table = table_name
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import aiohttp

        self._client = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        return self._client

    def _table_url(self) -> str:
        return f"{_API}/{self._base_id}/{self._table}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="airtable",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=50,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        key = self._chunk_key(encoded_chunk)
        payload = {
            "fields": {
                "Key": key,
                "Data": encoded_chunk,
            }
        }
        async with client.post(self._table_url(), json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        record_id = data["id"]
        loc = f"airtable://{self._base_id}/{self._table}/{record_id}"
        return StoredChunkRef(adapter_name="airtable", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        record_id = ref.location.rsplit("/", 1)[-1]
        async with client.get(f"{self._table_url()}/{record_id}") as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["fields"]["Data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        record_id = ref.location.rsplit("/", 1)[-1]
        try:
            async with client.delete(f"{self._table_url()}/{record_id}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        formula = f"SEARCH('{manifest_id}:', {{Key}}) > 0"
        params = {"filterByFormula": formula}
        refs: list[StoredChunkRef] = []
        try:
            async with client.get(self._table_url(), params=params) as resp:
                data = await resp.json()
            for record in data.get("records", []):
                loc = f"airtable://{self._base_id}/{self._table}/{record['id']}"
                refs.append(StoredChunkRef(adapter_name="airtable", location=loc))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            async with client.get(
                self._table_url(),
                params={"maxRecords": 1},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            client = await self._ensure_client()
            async with client.get(self._table_url()) as resp:
                data = await resp.json()
            count = len(data.get("records", []))
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": count,
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
