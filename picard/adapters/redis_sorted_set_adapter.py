"""Redis sorted set adapter -- stores chunks using Redis Sorted Set member scores.

Each write_chunk adds a member to a per-manifest sorted set. The encoded
chunk is the member value; the score is the write timestamp (for ordering).
This uses a different access pattern than Hash or plain string keys.

Requires: pip install redis[asyncio]
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_sorted_set"
_KEY_PREFIX = "picard:zset:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisSortedSetAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379,
                 password: str = "", db: int = 0) -> None:
        self._host = host
        self._port = port
        self._password = password or None
        self._db = db
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from redis.asyncio import Redis
            self._client = Redis(host=self._host, port=self._port,
                                 password=self._password, db=self._db,
                                 decode_responses=True)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=True,
            tags={"requires": "redis", "substrate": "redis-sorted-set"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"{_KEY_PREFIX}{manifest_id or 'default'}"
        score = time.time()
        # Use the chunk as member directly (Redis members are unique; prefix with uuid for dedup)
        member = f"{uuid.uuid4().hex[:8]}:{encoded_chunk}"
        client = await self._get_client()
        await client.zadd(key, {member: score})
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"key": key, "member": member}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        # Verify member exists and extract chunk (strip uuid prefix)
        score = await client.zscore(loc["key"], loc["member"])
        if score is None:
            raise KeyError(f"redis_sorted_set member not found: {loc['member']}")
        _, _, chunk = loc["member"].partition(":")
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.zrem(loc["key"], loc["member"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        key = f"{_KEY_PREFIX}{manifest_id}"
        client = await self._get_client()
        members = await client.zrange(key, 0, -1)
        return [
            StoredChunkRef(adapter_name=_NAME,
                           location=json.dumps({"key": key, "member": m}))
            for m in members
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            return await client.ping()
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
