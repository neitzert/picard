"""Redis RDB serialization adapter -- stores chunks via Redis DUMP/RESTORE.

Each write_chunk stores the encoded chunk in a temporary Redis key, DUMPs
the serialized RDB-format bytes, then DELetes the key. The RDB bytes are
stored as base64 in the location JSON. Cold reads RESTORE the key from the
stored bytes and GET the value.

Requires: pip install redis (async)
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-rdb"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class RedisModuleAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379,
                 password: str = "") -> None:
        self._host = host
        self._port = port
        self._password = password
        self._client = None
        self._reg: dict[str, str] = {}

    async def _get_client(self):
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.Redis(
                host=self._host, port=self._port,
                password=self._password or None,
                decode_responses=False,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-rdb-dump"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        client = await self._get_client()
        tmp_key = f"picard_tmp_{token}"
        await client.set(tmp_key, encoded_chunk.encode("utf-8"))
        raw = await client.dump(tmp_key)
        await client.delete(tmp_key)
        rdb_b64 = base64.b64encode(raw).decode("ascii") if raw else ""
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "rdb_b64": rdb_b64,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        rdb_b64 = loc.get("rdb_b64", "")
        if not rdb_b64:
            raise KeyError(f"redis-rdb chunk not found: {token}")
        raw = base64.b64decode(rdb_b64)
        client = await self._get_client()
        restore_key = f"picard_restore_{uuid.uuid4().hex[:8]}"
        await client.restore(restore_key, 0, raw, replace=True)
        chunk_bytes = await client.get(restore_key)
        await client.delete(restore_key)
        if chunk_bytes is None:
            raise KeyError(f"redis-rdb restore failed: {token}")
        chunk = chunk_bytes.decode("utf-8")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "rdb_b64": "",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            return await client.ping()
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
