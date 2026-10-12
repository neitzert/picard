"""Consul KV adapter — stores chunks as key-value entries in Consul.

Each chunk becomes a KV entry under a configurable prefix, keyed by
manifest/block/chunk identity.

Requires: pip install picard[consul]  (just httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ConsulAdapter(Adapter):
    """Scatter chunks into Consul KV store."""

    def __init__(
        self,
        *,
        endpoint: str = "http://localhost:8500",
        prefix: str = "picard/",
        token: str | None = None,
        max_chunk_bytes: int = 500_000,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._prefix = prefix
        self._token = token
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        headers: dict[str, str] = {}
        if self._token:
            headers["X-Consul-Token"] = self._token
        self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="consul",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        await client.put(
            f"{self._endpoint}/v1/kv/{key}",
            content=encoded_chunk.encode(),
        )
        return StoredChunkRef(adapter_name="consul", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(
            f"{self._endpoint}/v1/kv/{ref.location}",
            params={"raw": "true"},
        )
        resp.raise_for_status()
        return resp.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(
                f"{self._endpoint}/v1/kv/{ref.location}"
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"{self._prefix}{manifest_id}/"
        resp = await client.get(
            f"{self._endpoint}/v1/kv/{prefix}",
            params={"keys": "true"},
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        keys = resp.json()
        return [
            StoredChunkRef(adapter_name="consul", location=k)
            for k in keys
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._endpoint}/v1/status/leader")
            return resp.status_code == 200 and len(resp.text) > 0
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
