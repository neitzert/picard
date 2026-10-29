"""Redis Streams adapter -- stores chunks as Redis Stream entry fields.

Each write_chunk appends an entry to a stream named after the manifest.
The encoded chunk is stored in the 'data' field. Stream entry IDs
provide natural ordering and deduplication.

Requires: pip install redis[asyncio]
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_stream"
_STREAM_PREFIX = "picard:stream:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisStreamAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6379,
                 password: str = "", db: int = 0,
                 ttl_seconds: int = 86400 * 7) -> None:
        self._host = host
        self._port = port
        self._password = password or None
        self._db = db
        self._ttl = ttl_seconds
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
            ordered=True,
            ttl_seconds=self._ttl,
            tags={"requires": "redis", "substrate": "redis-stream-entry"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        stream_key = f"{_STREAM_PREFIX}{manifest_id or uuid.uuid4().hex[:8]}"
        client = await self._get_client()
        entry_id = await client.xadd(
            stream_key,
            {"data": encoded_chunk, "picard_manifest": manifest_id},
            maxlen=10000,
        )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"stream_key": stream_key,
                               "entry_id": entry_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        entries = await client.xrange(loc["stream_key"],
                                      min=loc["entry_id"],
                                      max=loc["entry_id"])
        if not entries:
            raise KeyError(f"redis_stream entry not found: {loc['entry_id']}")
        return entries[0][1]["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.xdel(loc["stream_key"], loc["entry_id"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        stream_key = f"{_STREAM_PREFIX}{manifest_id}"
        client = await self._get_client()
        entries = await client.xrange(stream_key)
        return [
            StoredChunkRef(adapter_name=_NAME,
                           location=json.dumps({"stream_key": stream_key,
                            "entry_id": eid}))
            for eid, fields in entries
            if fields.get("picard_manifest") == manifest_id
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
