"""Redis List adapter -- stores chunks as elements in a Redis List.

Each write_chunk pushes the encoded chunk to a per-manifest list. The
position (LPOS result) serves as the retrieval index. This gives ordered,
FIFO access to chunks within a manifest.

Requires: pip install redis[asyncio]
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_list"
_KEY_PREFIX = "picard:list:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisListAdapter(Adapter):

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
            tags={"requires": "redis", "substrate": "redis-list-element"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        # Prefix with uuid so identical chunks can coexist
        member = f"{uuid.uuid4().hex[:8]}:{encoded_chunk}"
        key = f"{_KEY_PREFIX}{manifest_id or 'default'}"
        client = await self._get_client()
        await client.rpush(key, member)
        # Get the index we just pushed to (LPOS finds last occurrence)
        idx = await client.lpos(key, member, rank=-1)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"key": key, "idx": idx,
                               "member": member}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        val = await client.lindex(loc["key"], loc["idx"])
        if val is None:
            raise KeyError(f"redis_list element not found at index {loc['idx']}")
        _, _, chunk = val.partition(":")
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        # LREM removes by value; count=1 removes first occurrence
        await client.lrem(loc["key"], 1, loc["member"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        key = f"{_KEY_PREFIX}{manifest_id}"
        client = await self._get_client()
        members = await client.lrange(key, 0, -1)
        return [
            StoredChunkRef(adapter_name=_NAME,
                           location=json.dumps({"key": key, "idx": i, "member": m}))
            for i, m in enumerate(members)
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
