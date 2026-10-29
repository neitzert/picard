"""Valkey adapter -- stores chunks in a Valkey instance (Redis-compatible fork)."""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "valkey"
_TTL = 86400
_KEY_PREFIX = "picard:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class ValkeyAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6380,
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
            self._client = Redis(
                host=self._host, port=self._port,
                password=self._password, db=self._db,
                decode_responses=True,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=512_000_000,
            persistent=True,
            ordered=False,
            ttl_seconds=_TTL,
            rate_limit_per_min=100000,
            tags={"requires": "redis-py", "substrate": "valkey-string"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        key = f"{_KEY_PREFIX}{token}"
        self._reg[token] = encoded_chunk
        client = await self._get_client()
        await client.set(key, encoded_chunk, ex=_TTL)
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        client = await self._get_client()
        val = await client.get(loc["key"])
        if val is None:
            raise KeyError(f"valkey chunk not found: {token}")
        self._reg[token] = val
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.delete(loc["key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        refs: list[StoredChunkRef] = []
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor, match=f"{_KEY_PREFIX}*",
                                              count=100)
            for key in keys:
                val = await client.get(key)
                if val and _manifest_id_from_chunk(val) == manifest_id:
                    token = key[len(_KEY_PREFIX):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "key": key}),
                    ))
            if cursor == 0:
                break
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
