"""Redis Geo adapter -- stores chunks encoded as geospatial member names.

Each write_chunk adds a member to a Redis Geo set where the member name
carries the encoded chunk. Longitude/latitude are derived from the manifest
to give a stable location for list scans. Member names carry the payload
with a uuid prefix to ensure uniqueness.

Requires: pip install redis
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "redis_geo"
_KEY_PREFIX = "picard:geo:"
_LON = 13.404954
_LAT = 52.520008


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class RedisGeoAdapter(Adapter):

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
            max_chunk_bytes=256,
            persistent=True,
            ordered=False,
            tags={"requires": "redis", "substrate": "redis-geo-member"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        token = uuid.uuid4().hex[:8]
        member = f"{token}:{encoded_chunk}"
        key = f"{_KEY_PREFIX}{manifest_id or 'default'}"
        client = await self._get_client()
        await client.geoadd(key, [_LON, _LAT, member])
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"key": key, "member": member,
                               "token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        client = await self._get_client()
        # GEOPOS confirms member exists; the chunk is in the member name itself
        pos = await client.geopos(loc["key"], loc["member"])
        if not pos or pos[0] is None:
            raise KeyError(f"redis_geo member not found: {loc['member']}")
        _, _, chunk = loc["member"].partition(":")
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        client = await self._get_client()
        await client.zrem(loc["key"], loc["member"])
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        key = f"{_KEY_PREFIX}{manifest_id}"
        client = await self._get_client()
        members = await client.zrange(key, 0, -1)
        refs = []
        for m in members:
            _, _, chunk = m.partition(":")
            if _manifest_id_from_chunk(chunk) == manifest_id:
                token = m.split(":")[0]
                refs.append(StoredChunkRef(adapter_name=_NAME,
                                           location=json.dumps({"key": key,
                                            "member": m, "token": token})))
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
