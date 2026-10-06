"""Multicast adapter — chunks broadcast via IP multicast groups.

Joins a multicast group and sends chunk data as UDP datagrams.
Any receiver on the same network segment (or with multicast routing)
can silently capture the data. No central server needed.

Requires: UDP socket access, multicast-capable network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MulticastAdapter(Adapter):
    """Scatter chunks via IP multicast datagrams."""

    def __init__(
        self,
        group: str = "239.255.42.42",
        port: int = 5007,
        ttl: int = 2,
        *,
        max_chunk_bytes: int = 1400,
    ) -> None:
        self._group = group
        self._port = port
        self._ttl = ttl
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="multicast",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=600,
            ttl_seconds=None,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        message = json.dumps({
            "key": chunk_key,
            "data": base64.b64encode(encoded_chunk.encode()).decode(),
        }).encode()

        def _send():
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
                sock.sendto(message, (self._group, self._port))
            finally:
                sock.close()

        try:
            await asyncio.to_thread(_send)
        except Exception:
            pass

        self._cache[chunk_key] = encoded_chunk
        return StoredChunkRef(adapter_name="multicast", location=chunk_key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"Multicast record not cached: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(adapter_name="multicast", location=k)
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self._ttl)
            sock.close()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
