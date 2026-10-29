"""Redis Hash adapter -- stores chunks as field-value pairs in a Redis Hash.

Each write_chunk stores the encoded chunk in a per-manifest Hash under a
UUID key. Compared to plain string keys, this groups chunks by manifest and
uses the Hash field as the lookup key.

Requires: pip install redis[asyncio]
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_hash"
_HASH_PREFIX = "picard:hash:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisHashAdapter(Adapter):

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
            tags={"requires": "redis", "substrate": "redis-hash-field"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        hash_key = f"{_HASH_PREFIX}{manifest_id or 'default'}"
        field = uuid.uuid4().hex
        client = await self._get_client()
        await client.hset(hash_key, field, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"hash_key": hash_key,
                               "field": field}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        val = await client.hget(loc["hash_key"], loc["field"])
        if val is None:
            raise KeyError(f"redis_hash field not found: {loc['field']}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.hdel(loc["hash_key"], loc["field"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        hash_key = f"{_HASH_PREFIX}{manifest_id}"
        client = await self._get_client()
        fields = await client.hkeys(hash_key)
        return [
            StoredChunkRef(adapter_name=_NAME,
                           location=json.dumps({"hash_key": hash_key, "field": f}))
            for f in fields
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
