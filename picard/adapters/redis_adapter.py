"""Redis adapter — stores chunks as Redis string keys.

Each chunk becomes a key-value pair in Redis.  Supports optional TTL
for automatic expiry.

Requires: pip install picard[redis]
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class RedisAdapter(Adapter):
    """Scatter chunks into Redis string keys."""

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        *,
        prefix: str = "picard:",
        ttl_seconds: int | None = None,
        max_chunk_bytes: int = 500_000_000,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(self._url)
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}:b{block}:c{chunk_index}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="redis",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
            ttl_seconds=self._ttl,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        kwargs: dict = {}
        if self._ttl is not None:
            kwargs["ex"] = self._ttl
        await client.set(key, encoded_chunk, **kwargs)
        return StoredChunkRef(adapter_name="redis", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        value = await client.get(ref.location)
        if value is None:
            raise ValueError(f"chunk not found: {ref.location}")
        if isinstance(value, bytes):
            return value.decode()
        return value

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        result = await client.delete(ref.location)
        return result > 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        pattern = f"{self._prefix}{manifest_id}:*"
        keys = await client.keys(pattern)
        refs: list[StoredChunkRef] = []
        for k in keys:
            key_str = k.decode() if isinstance(k, bytes) else k
            refs.append(StoredChunkRef(adapter_name="redis", location=key_str))
        return refs

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            return await client.ping()
        except Exception:
            return False
