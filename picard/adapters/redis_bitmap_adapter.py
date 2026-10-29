"""Redis Bitmap adapter -- stores chunks as bit sequences in a Redis bitmap key.

Each write_chunk encodes the chunk bytes as individual bits via SETBIT.
Cold reads reconstruct bytes from GETBIT calls.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-bitmap"
_KEY_PREFIX = "picard:bm:"


def _manifest_id_from_chunk(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisBitmapAdapter(Adapter):

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
                                 decode_responses=False)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=256,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-bitmap"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        key = f"{_KEY_PREFIX}{token}"
        raw = encoded_chunk.encode("utf-8")
        bit_count = len(raw) * 8
        client = await self._get_client()
        pipe = client.pipeline(transaction=True)
        for byte_idx, byte_val in enumerate(raw):
            for bit_idx in range(8):
                bit = (byte_val >> (7 - bit_idx)) & 1
                pipe.setbit(key, byte_idx * 8 + bit_idx, bit)
        await pipe.execute()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key, "bits": bit_count}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        key = loc["key"]
        bit_count = loc["bits"]
        byte_count = bit_count // 8
        client = await self._get_client()
        pipe = client.pipeline(transaction=False)
        for offset in range(bit_count):
            pipe.getbit(key, offset)
        bits = await pipe.execute()
        raw_bytes = bytearray()
        for i in range(byte_count):
            byte_val = 0
            for j in range(8):
                byte_val = (byte_val << 1) | bits[i * 8 + j]
            raw_bytes.append(byte_val)
        return raw_bytes.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.delete(loc["key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        keys = []
        async for key in client.scan_iter(match=f"{_KEY_PREFIX}*"):
            keys.append(key)
        refs = []
        for key in keys:
            key_str = key.decode("utf-8") if isinstance(key, bytes) else key
            token = key_str[len(_KEY_PREFIX):]
            if token in self._reg:
                chunk = self._reg[token]
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    bit_count = len(chunk.encode("utf-8")) * 8
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "key": key_str, "bits": bit_count}),
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
