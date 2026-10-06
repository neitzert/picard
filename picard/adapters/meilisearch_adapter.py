"""Meilisearch adapter — stores chunks as search documents.

Hides data in a lightweight search engine. Meilisearch is increasingly
popular for site search — chunks blend into the search index.

Requires: pip install picard[meilisearch]  (aiohttp)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MeilisearchAdapter(Adapter):
    """Scatter chunks into Meilisearch documents."""

    def __init__(
        self,
        url: str = "http://localhost:7700",
        api_key: str | None = None,
        index: str = "picard",
        *,
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._index = index
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="meilisearch",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}-{chunk.block_index}-{chunk.chunk_index}"
            manifest_id = chunk.manifest_id
        except ValueError:
            key = uuid.uuid4().hex
            manifest_id = ""

        doc = [{"id": key, "data": encoded_chunk, "manifest_id": manifest_id}]
        url = f"{self._url}/indexes/{self._index}/documents"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=self._headers(), data=json.dumps(doc)
            ) as resp:
                resp.raise_for_status()

        loc = f"meili://{self._index}/{key}"
        return StoredChunkRef(adapter_name="meilisearch", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("meili://").split("/", 1)
        index, key = parts[0], parts[1]
        url = f"{self._url}/indexes/{index}/documents/{key}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status == 404:
                    raise KeyError(f"chunk not found: {ref.location}")
                resp.raise_for_status()
                body = await resp.json()
                return body["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("meili://").split("/", 1)
        index, key = parts[0], parts[1]
        url = f"{self._url}/indexes/{index}/documents/{key}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=self._headers()) as resp:
                    return resp.status in (200, 202)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"{self._url}/indexes/{self._index}/search"
        payload = {
            "q": "",
            "filter": f"manifest_id = '{manifest_id}'",
            "limit": 10000,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self._headers(), data=json.dumps(payload)
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
                    return [
                        StoredChunkRef(
                            adapter_name="meilisearch",
                            location=f"meili://{self._index}/{hit['id']}",
                        )
                        for hit in body.get("hits", [])
                    ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/health", headers=self._headers()
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._url}/indexes/{self._index}/stats",
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
                    return {
                        "used_bytes": body.get("rawDocumentDbSize", None),
                        "free_bytes": None,
                        "total_bytes": None,
                        "chunk_count": body.get("numberOfDocuments", None),
                    }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
