"""Elasticsearch adapter — stores chunks as search documents.

Stores data in the search/observability infrastructure as indexed documents.

Requires: pip install picard[elasticsearch]  (aiohttp)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ElasticsearchAdapter(Adapter):
    """Scatter chunks into Elasticsearch documents."""

    def __init__(
        self,
        hosts: list[str] | None = None,
        index: str = "picard-chunks",
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        max_chunk_bytes: int = 104_857_600,
    ) -> None:
        self._hosts = hosts or ["http://localhost:9200"]
        self._index = index
        self._api_key = api_key
        self._username = username
        self._password = password
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="elasticsearch",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=3000,
        )

    def _base(self) -> str:
        return self._hosts[0].rstrip("/")

    def _auth(self) -> aiohttp.BasicAuth | None:
        if self._username and self._password:
            return aiohttp.BasicAuth(self._username, self._password)
        return None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"ApiKey {self._api_key}"
        return h

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}-{chunk.block_index}-{chunk.chunk_index}"
            manifest_id = chunk.manifest_id
        except ValueError:
            key = uuid.uuid4().hex
            manifest_id = ""

        doc = {"data": encoded_chunk, "manifest_id": manifest_id}
        url = f"{self._base()}/{self._index}/_doc/{key}"

        async with aiohttp.ClientSession(auth=self._auth()) as session:
            async with session.put(
                url, headers=self._headers(), data=json.dumps(doc)
            ) as resp:
                resp.raise_for_status()

        loc = f"es://{self._index}/{key}"
        return StoredChunkRef(adapter_name="elasticsearch", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("es://").split("/", 1)
        index, key = parts[0], parts[1]
        url = f"{self._base()}/{index}/_doc/{key}"

        async with aiohttp.ClientSession(auth=self._auth()) as session:
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status == 404:
                    raise KeyError(f"chunk not found: {ref.location}")
                resp.raise_for_status()
                body = await resp.json()
                return body["_source"]["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("es://").split("/", 1)
        index, key = parts[0], parts[1]
        url = f"{self._base()}/{index}/_doc/{key}"

        try:
            async with aiohttp.ClientSession(auth=self._auth()) as session:
                async with session.delete(url, headers=self._headers()) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"{self._base()}/{self._index}/_search"
        query = {
            "query": {"term": {"manifest_id": manifest_id}},
            "size": 10000,
            "_source": False,
        }

        async with aiohttp.ClientSession(auth=self._auth()) as session:
            async with session.post(
                url, headers=self._headers(), data=json.dumps(query)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
                return [
                    StoredChunkRef(
                        adapter_name="elasticsearch",
                        location=f"es://{self._index}/{hit['_id']}",
                    )
                    for hit in body.get("hits", {}).get("hits", [])
                ]

    async def health_check(self) -> bool:
        try:
            url = f"{self._base()}/_cluster/health"
            async with aiohttp.ClientSession(auth=self._auth()) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            url = f"{self._base()}/{self._index}/_stats"
            async with aiohttp.ClientSession(auth=self._auth()) as session:
                async with session.get(url, headers=self._headers()) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
                    total = body["_all"]["total"]
                    return {
                        "used_bytes": total["store"]["size_in_bytes"],
                        "free_bytes": None,
                        "total_bytes": None,
                        "chunk_count": total["docs"]["count"],
                    }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
