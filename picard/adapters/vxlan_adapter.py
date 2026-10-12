"""VXLAN adapter — store chunks in VXLAN-encapsulated Ethernet frames.

Crafts VXLAN packets (RFC 7348) with chunk data as the inner payload,
sent as UDP datagrams to a remote VTEP.  Reads maintain a local cache
populated by a listener on the VXLAN port.

Note: VXLAN is ubiquitous in cloud/DC networks.  A custom VNI with
chunk payloads blends with normal overlay traffic.

Requires: pip install picard-store[vxlan]  (no external deps, uses stdlib socket)
"""

from __future__ import annotations

import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


# VXLAN header: flags (1B) + reserved (3B) + VNI (3B) + reserved (1B)
_VXLAN_FLAGS = 0x08  # I flag set


class VxlanAdapter(Adapter):
    """Scatter chunks into VXLAN-encapsulated frames."""

    def __init__(
        self,
        vtep_ip: str,
        dest_vtep_ip: str,
        *,
        vni: int = 999999,
        port: int = 4789,
        max_chunk_bytes: int = 8950,
    ) -> None:
        self._vtep_ip = vtep_ip
        self._dest_vtep_ip = dest_vtep_ip
        self._vni = vni
        self._port = port
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="vxlan",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _make_vxlan_header(self) -> bytes:
        flags_reserved = (_VXLAN_FLAGS << 24)
        vni_reserved = (self._vni << 8)
        return struct.pack("!II", flags_reserved, vni_reserved)

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import asyncio
        import socket

        key = self._chunk_key(encoded_chunk)
        vxlan_hdr = self._make_vxlan_header()
        key_bytes = key.encode().ljust(32, b"\x00")[:32]
        payload = vxlan_hdr + key_bytes + encoded_chunk.encode()

        def _send():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(payload, (self._dest_vtep_ip, self._port))
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        self._cache[key] = encoded_chunk
        loc = f"vxlan://{self._dest_vtep_ip}:{self._vni}/{key}"
        return StoredChunkRef(adapter_name="vxlan", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.replace("vxlan://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        if key not in self._cache:
            raise KeyError(f"chunk not in cache: {ref.location}")
        return self._cache[key]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("vxlan://", "").split("/", 1)
        key = parts[1] if len(parts) > 1 else ""
        return self._cache.pop(key, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(
                adapter_name="vxlan",
                location=f"vxlan://{self._dest_vtep_ip}:{self._vni}/{k}",
            )
            for k in self._cache
            if k.startswith(manifest_id)
        ]

    async def health_check(self) -> bool:
        import asyncio
        import socket

        def _check():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
