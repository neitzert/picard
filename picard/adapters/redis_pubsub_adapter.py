"""Redis Pub/Sub adapter -- stores chunks via Redis PUBLISH/SUBSCRIBE.

Because pub/sub messages are ephemeral, chunks are also persisted in a
companion Redis Hash keyed by chunk token. The channel name encodes the
manifest. list_chunks scans the hash for the manifest.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_pubsub"
_STORE_KEY = "picard:pubsub:store"
_CHAN_PREFIX = "picard:chan:"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisPubsubAdapter(Adapter):

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
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-pubsub-channel"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        token = uuid.uuid4().hex[:16]
        channel = f"{_CHAN_PREFIX}{manifest_id or 'default'}"
        client = await self._get_client()
        # Persist in hash for retrieval; also publish to channel
        field = f"{token}:{manifest_id}"
        await client.hset(_STORE_KEY, field, encoded_chunk)
        await client.publish(channel, json.dumps({"token": token, "chunk": encoded_chunk}))
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                               "manifest": manifest_id, "field": field}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        val = await client.hget(_STORE_KEY, loc["field"])
        if val is None:
            raise KeyError(f"redis_pubsub chunk not found: {loc['token']}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.hdel(_STORE_KEY, loc["field"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        all_fields = await client.hgetall(_STORE_KEY)
        refs = []
        for field, chunk in all_fields.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                token, _, mid = field.partition(":")
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({
                                               "token": token,
                                               "manifest": mid,
                                               "field": field,
                                           })))
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
