"""RedisJSON adapter -- stores chunks as JSON documents via the RedisJSON module.

Each write_chunk stores the encoded chunk at JSON path $.chunk using
JSON.SET. Requires Redis with the RedisJSON module (Redis Stack or
redis/redis-stack Docker image).

Requires: pip install redis[hiredis]
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-json"
_PREFIX = "picard:json:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisJsonAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6379,
        password: str = "",
        db: int = 0,
    ) -> None:
        self._host = host
        self._port = port
        self._password = password or None
        self._db = db
        self._client = None
        self._reg: dict[str, str] = {}

    async def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.Redis(
                host=self._host, port=self._port,
                password=self._password, db=self._db,
                decode_responses=True,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
            tags={"requires": "redis[hiredis],redisjson-module",
                  "substrate": "redis-json"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"{_PREFIX}{token}"
        doc = json.dumps({
            "token": token,
            "chunk": encoded_chunk,
            "manifest": manifest_id,
        })
        client = await self._get_client()
        await client.execute_command("JSON.SET", key, "$", doc)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        client = await self._get_client()
        result = await client.execute_command(
            "JSON.GET", loc["key"], "$.chunk"
        )
        if result is None:
            raise KeyError(f"redis-json chunk not found: {token}")
        chunks = json.loads(result)
        chunk = chunks[0] if isinstance(chunks, list) else chunks
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        client = await self._get_client()
        await client.execute_command("JSON.DEL", loc["key"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        keys = await client.keys(f"{_PREFIX}*")
        refs = []
        for key in keys:
            try:
                result = await client.execute_command(
                    "JSON.GET", key, "$.manifest"
                )
                if result is None:
                    continue
                manifests = json.loads(result)
                mid = manifests[0] if isinstance(manifests, list) else manifests
                if mid == manifest_id:
                    token = key[len(_PREFIX):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "key": key,
                                              "manifest": manifest_id}),
                    ))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            await client.ping()
            await client.execute_command("JSON.SET", "__picard_hc__", "$",
                                          '"ok"')
            await client.delete("__picard_hc__")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
