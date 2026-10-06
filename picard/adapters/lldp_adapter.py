"""LLDP adapter -- stores chunks in custom organizationally-specific TLVs.

Link Layer Discovery Protocol frames are broadcast on the LAN.
Custom TLV type 127 allows organizational extensions with arbitrary data.
Very low bandwidth but essentially invisible on most networks.

Requires: pip install picard[lldp]  (scapy)
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class LldpAdapter(Adapter):
    """Scatter chunks as LLDP organizationally-specific TLVs."""

    def __init__(
        self,
        interface: str,
        *,
        oui: str = "000000",
        max_chunk_bytes: int = 507,
    ) -> None:
        self._interface = interface
        self._oui = bytes.fromhex(oui)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._subtype_counter = 1

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="lldp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=2,
            ttl_seconds=120,
        )

    def _build_frame(self, subtype: int, payload: bytes):
        from scapy.all import Ether

        lldp_multicast = "01:80:c2:00:00:0e"
        tlv_value = self._oui + struct.pack("B", subtype) + payload
        tlv_type = 127
        tlv_len = len(tlv_value)
        tlv_header = struct.pack("!H", (tlv_type << 9) | tlv_len)
        end_tlv = struct.pack("!H", 0)

        chassis_id = struct.pack("!H", (1 << 9) | 2) + b"\x04\x01"
        port_id = struct.pack("!H", (2 << 9) | 2) + b"\x07\x01"
        ttl_tlv = struct.pack("!H", (3 << 9) | 2) + struct.pack("!H", 120)

        raw = chassis_id + port_id + ttl_tlv + tlv_header + tlv_value + end_tlv

        frame = Ether(dst=lldp_multicast, type=0x88CC) / raw
        return frame

    def _send_frame(self, frame) -> None:
        from scapy.all import sendp

        sendp(frame, iface=self._interface, verbose=False)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_id = uuid.uuid4().hex

        subtype = self._subtype_counter
        self._subtype_counter = (self._subtype_counter % 255) + 1

        payload = encoded_chunk.encode("utf-8")
        frame = self._build_frame(subtype, payload)
        await asyncio.to_thread(self._send_frame, frame)

        self._cache[chunk_id] = encoded_chunk
        location = f"lldp://{self._interface}/{subtype}/{chunk_id}"
        return StoredChunkRef(adapter_name="lldp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        val = self._cache.get(chunk_id)
        if val is None:
            raise KeyError(f"lldp chunk not found: {chunk_id}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        return self._cache.pop(chunk_id, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(
                adapter_name="lldp",
                location=f"lldp://{self._interface}/0/{cid}",
            )
            for cid in self._cache
            if cid.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            import psutil

            return self._interface in psutil.net_if_addrs()
        except ImportError:
            return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
