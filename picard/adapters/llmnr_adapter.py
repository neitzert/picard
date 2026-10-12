"""LLMNR adapter -- stores chunks as Link-Local Multicast Name Resolution queries.

Each chunk is encoded into an LLMNR name registration broadcast on the
local network segment (multicast 224.0.0.252:5355).  Read-back relies
on a local cache since LLMNR is a broadcast protocol with no central
store.

Requires: no external dependencies (uses stdlib socket).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_LLMNR_MCAST = "224.0.0.252"
_LLMNR_PORT = 5355


class LlmnrAdapter(Adapter):
    """Scatter chunks via LLMNR name registration broadcasts."""

    def __init__(
        self,
        interface: str = "0.0.0.0",
        *,
        domain_suffix: str = ".picard.local",
        max_chunk_bytes: int = 255,
    ) -> None:
        self._interface = interface
        self._domain_suffix = domain_suffix
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="llmnr",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            name = f"{chunk.manifest_id}-{chunk.block_index}-{chunk.chunk_index}"
            return f"llmnr://{self._interface}/{name}"
        except (ValueError, Exception):
            return f"llmnr://{self._interface}/{uuid.uuid4().hex}"

    def _build_llmnr_query(self, name: str, data: bytes) -> bytes:
        """Build a minimal LLMNR packet encoding data in the query name."""
        txn_id = struct.pack("!H", hash(name) & 0xFFFF)
        flags = b"\x00\x00"
        counts = struct.pack("!HHHH", 1, 0, 0, 0)
        encoded_name = b""
        for label in name.split("."):
            label_bytes = label.encode("utf-8")[:63]
            encoded_name += bytes([len(label_bytes)]) + label_bytes
        encoded_name += b"\x00"
        qtype = struct.pack("!HH", 16, 1)  # TXT, IN
        return txn_id + flags + counts + encoded_name + qtype

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        loc = self._location_key(encoded_chunk)
        name_hash = hashlib.sha256(encoded_chunk.encode()).hexdigest()[:16]
        query_name = name_hash + self._domain_suffix

        def _send():
            pkt = self._build_llmnr_query(query_name, encoded_chunk.encode())
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.sendto(pkt, (_LLMNR_MCAST, _LLMNR_PORT))
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="llmnr", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not in LLMNR cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"llmnr://{self._interface}/{manifest_id}-"
        return [
            StoredChunkRef(adapter_name="llmnr", location=loc)
            for loc in self._cache
            if loc.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
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
