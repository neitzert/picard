"""Redis HyperLogLog adapter -- stores chunks via PFADD with a companion data key.

The HLL key acts as a cardinality commitment; a companion plain-string key
holds the chunk for cold reads.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-hyperloglog"
_HLL_PREFIX = "picard:hll:"
_DATA_PREFIX = "picard:hll:data:"


def _manifest_id_from_chunk(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisHyperloglogAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379,
                 password: str = "", db: int = 0) -> None:
        self._host = host
        self._port = port
        self._password = password or None
        self._db = db
        self._client = None
        self._reg: dict[str, str] = {}

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
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-hyperloglog"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        hll_key = f"{_HLL_PREFIX}{token}"
        data_key = f"{_DATA_PREFIX}{token}"
        client = await self._get_client()
        await client.pfadd(hll_key, encoded_chunk)
        await client.set(data_key, encoded_chunk)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "hll_key": hll_key, "data_key": data_key}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        client = await self._get_client()
        val = await client.get(loc["data_key"])
        if val is None:
            raise KeyError(f"redis-hyperloglog chunk not found: {token}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.delete(loc["hll_key"], loc["data_key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        refs = []
        async for data_key in client.scan_iter(match=f"{_DATA_PREFIX}*"):
            token = data_key[len(_DATA_PREFIX):]
            chunk = await client.get(data_key)
            if chunk and _manifest_id_from_chunk(chunk) == manifest_id:
                hll_key = f"{_HLL_PREFIX}{token}"
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "hll_key": hll_key, "data_key": data_key}),
                ))
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
