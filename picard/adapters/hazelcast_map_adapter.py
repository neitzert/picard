"""Hazelcast map adapter -- stores chunks in a Hazelcast IMap.

Each write_chunk puts the encoded chunk into a Hazelcast distributed map
named "picard-chunks". Keys are tokens; values are the encoded chunks.
Hazelcast provides in-memory distributed storage with optional persistence.

Requires: pip install hazelcast-python-client
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "hazelcast-map"
_MAP_NAME = "picard-chunks"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class HazelcastMapAdapter(Adapter):

    def __init__(
        self,
        *,
        cluster_name: str = "dev",
        cluster_members: list[str] | None = None,
    ) -> None:
        self._cluster_name = cluster_name
        self._cluster_members = cluster_members or ["127.0.0.1:5701"]
        self._client = None
        self._map = None
        self._reg: dict[str, str] = {}

    async def _get_map(self):
        if self._client is None:
            import hazelcast
            self._client = hazelcast.HazelcastClient(
                cluster_name=self._cluster_name,
                cluster_members=self._cluster_members,
            )
        if self._map is None:
            self._map = await asyncio.to_thread(
                self._client.get_map, _MAP_NAME
            )
        return self._map

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=False,
            ordered=False,
            rate_limit_per_min=100000,
            tags={"requires": "hazelcast-python-client", "substrate": "hazelcast-imap"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        hz_map = await self._get_map()
        await asyncio.to_thread(hz_map.put, token, encoded_chunk)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        hz_map = await self._get_map()
        chunk = await asyncio.to_thread(hz_map.get, token)
        if chunk is None:
            raise KeyError(f"hazelcast-map chunk not found: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        try:
            hz_map = await self._get_map()
            await asyncio.to_thread(hz_map.delete, token)
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            hz_map = await self._get_map()
            await asyncio.to_thread(hz_map.size)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._client is not None:
            await asyncio.to_thread(self._client.shutdown)
            self._client = None
            self._map = None
