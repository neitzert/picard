"""Redis Set adapter -- stores chunks as set members in Redis.

Each write_chunk adds a member f"{uuid8}:{chunk}" to a manifest-keyed set.
The member is self-describing: prefix ensures uniqueness; suffix is the chunk.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_set"
_KEY_PREFIX = "picard:set:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisSetAdapter(Adapter):

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
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-set-member"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        token = uuid.uuid4().hex[:8]
        member = f"{token}:{encoded_chunk}"
        key = f"{_KEY_PREFIX}{manifest_id or 'default'}"
        client = await self._get_client()
        await client.sadd(key, member)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"key": key, "member": member}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        exists = await client.sismember(loc["key"], loc["member"])
        if not exists:
            raise KeyError(f"redis_set member not found: {loc['member']}")
        _, _, chunk = loc["member"].partition(":")
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.srem(loc["key"], loc["member"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        key = f"{_KEY_PREFIX}{manifest_id}"
        client = await self._get_client()
        members = await client.smembers(key)
        refs = []
        for m in members:
            _, _, chunk = m.partition(":")
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"key": key,
                                            "member": m})))
        return refs

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
