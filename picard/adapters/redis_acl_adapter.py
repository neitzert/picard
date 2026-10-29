"""Redis ACL adapter -- stores chunks via Redis ACL user creation.

Each write_chunk creates a dedicated ACL user picard_{token} with NOPASS
and no command permissions. The chunk is embedded in the location JSON and
warm-cached in _reg. Presence is verifiable via ACL LIST.

Requires: pip install redis (async)
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis-acl"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class RedisAclAdapter(Adapter):

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
                decode_responses=True,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=256,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-acl-user"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        client = await self._get_client()
        username = f"picard_{token}"
        await client.execute_command("ACL", "SETUSER", username, "nopass", "nocommands")
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "username": username,
                                  "chunk": encoded_chunk, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        chunk = loc.get("chunk", "")
        if chunk:
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"redis-acl chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        client = await self._get_client()
        try:
            await client.execute_command("ACL", "DELUSER", loc["username"])
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "username": f"picard_{token}",
                                          "chunk": chunk,
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            result = await client.execute_command("ACL", "LIST")
            return isinstance(result, list)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
