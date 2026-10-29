"""Redis Keyspace adapter -- encodes chunk data in the Redis key name itself.

The chunk is base64url-encoded and written as `PICARD:{token}:{b64}` with a
24-hour TTL. Cold reads use SCAN to find the key by token prefix.

Requires: pip install redis
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-keyspace"
_TTL = 86400


def _manifest_id_from_chunk(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _encode(chunk: str) -> str:
    return base64.urlsafe_b64encode(chunk.encode("utf-8")).decode("ascii")


def _decode(b64: str) -> str:
    return base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8")


class RedisKeyspaceAdapter(Adapter):

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
            max_chunk_bytes=512,
            persistent=True,
            ordered=False,
            ttl_seconds=_TTL,
            tags={"requires": "redis", "substrate": "redis-keyspace"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:8]
        b64 = _encode(encoded_chunk)
        key = f"PICARD:{token}:{b64}"
        client = await self._get_client()
        await client.set(key, "1", ex=_TTL)
        self._reg[token] = encoded_chunk
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
        async for key in client.scan_iter(match=f"PICARD:{token}:*"):
            _, _, b64 = key.split(":", 2)
            return _decode(b64)
        raise KeyError(f"redis-keyspace chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.delete(loc["key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        refs = []
        async for key in client.scan_iter(match="PICARD:*"):
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            _, token, b64 = parts
            try:
                chunk = _decode(b64)
            except Exception:
                continue
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "key": key}),
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
