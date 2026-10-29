"""Redis Lua script adapter -- stores chunks as registered Lua scripts.

Each write_chunk uses SCRIPT LOAD to register the encoded chunk as a Lua
script. Redis returns a SHA1 digest that serves as the script handle.
The actual chunk content is embedded in the Lua script as a comment so it
survives across server restarts (when SCRIPT FLUSH is not called).

Requires: pip install redis[hiredis]
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-lua"
_PREFIX = "picard:lua:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _wrap_chunk(token: str, manifest_id: str, encoded_chunk: str) -> str:
    safe = encoded_chunk.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f'-- picard:{token}:{manifest_id}\n'
        f'return "{safe}"\n'
    )


def _unwrap_chunk(script: str) -> str:
    for line in script.splitlines():
        if line.startswith("return "):
            val = line[len("return "):]
            if val.startswith('"') and val.endswith('"'):
                return val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return ""


class RedisLuaAdapter(Adapter):

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
        self._reg: dict[str, tuple[str, str]] = {}

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
            max_chunk_bytes=32768,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10000,
            tags={"requires": "redis[hiredis]",
                  "substrate": "redis-lua-script"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        script = _wrap_chunk(token, manifest_id, encoded_chunk)
        client = await self._get_client()
        sha = await client.script_load(script)
        self._reg[token] = (sha, encoded_chunk)
        key = f"{_PREFIX}{token}"
        await client.set(key, json.dumps({
            "sha": sha, "manifest": manifest_id, "script": script,
        }))
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "sha": sha,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]
        client = await self._get_client()
        key = f"{_PREFIX}{token}"
        raw = await client.get(key)
        if not raw:
            raise KeyError(f"redis-lua chunk not found: {token}")
        meta = json.loads(raw)
        chunk = _unwrap_chunk(meta["script"])
        self._reg[token] = (meta["sha"], chunk)
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        client = await self._get_client()
        await client.delete(f"{_PREFIX}{token}")
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        keys = await client.keys(f"{_PREFIX}*")
        refs = []
        for key in keys:
            try:
                raw = await client.get(key)
                if not raw:
                    continue
                meta = json.loads(raw)
                if meta.get("manifest") == manifest_id:
                    token = str(key)[len(_PREFIX):]
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": token, "sha": meta["sha"],
                                              "manifest": manifest_id}),
                    ))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            await client.ping()
            sha = await client.script_load("return 1")
            return bool(sha)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
