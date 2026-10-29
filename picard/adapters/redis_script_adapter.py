"""Redis Lua script adapter -- stores chunks as cached Lua scripts.

Each write_chunk loads a Lua script (containing the encoded chunk as a
comment) via SCRIPT LOAD. The returned SHA1 hash serves as the key.
The actual chunk is also stored in a companion Redis String for retrieval.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_script"
_META_KEY = "picard:script:meta"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisScriptAdapter(Adapter):

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
            max_chunk_bytes=100_000,
            persistent=False,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-script-cache"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        # Embed chunk in a Lua comment so the script is unique per chunk
        lua = f"-- {encoded_chunk}\nreturn 1"
        client = await self._get_client()
        sha = await client.script_load(lua)
        # Store metadata so we can retrieve the chunk and list by manifest
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        await client.hset(_META_KEY, f"{sha}:{token}", json.dumps({
            "chunk": encoded_chunk,
            "manifest": manifest_id,
            "sha": sha,
            "token": token,
        }))
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"sha": sha, "token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        raw = await client.hget(_META_KEY, f"{loc['sha']}:{loc['token']}")
        if raw is None:
            raise KeyError(f"redis_script chunk not found: sha={loc['sha']}")
        return json.loads(raw)["chunk"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.script_flush()
        await client.hdel(_META_KEY, f"{loc['sha']}:{loc['token']}")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        all_meta = await client.hgetall(_META_KEY)
        refs = []
        for key, val in all_meta.items():
            try:
                obj = json.loads(val)
                if obj.get("manifest") == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({
                                                   "sha": obj["sha"],
                                                   "token": obj["token"],
                                               })))
            except Exception:
                continue
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
