"""ARP adapter -- stores tiny chunks in gratuitous ARP announcements.

Each chunk is encoded into the hardware address padding of a
gratuitous ARP packet and cached locally for read-back.  This is
a steganographic channel: extremely small capacity, completely
ephemeral, but very hard to detect or block on a LAN.

Requires: pip install picard[arp]  (scapy)
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ArpAdapter(Adapter):
    """Scatter tiny chunks via gratuitous ARP on the local LAN."""

    def __init__(
        self,
        interface: str,
        *,
        network: str = "192.168.1.0/24",
        max_chunk_bytes: int = 128,
    ) -> None:
        self._interface = interface
        self._network = network
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="arp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            return (
                f"arp://{self._interface}/"
                f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
            )
        except (ValueError, Exception):
            return f"arp://{self._interface}/{uuid.uuid4().hex}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from scapy.all import ARP, Ether, sendp

        loc = self._location_key(encoded_chunk)
        chunk_hash = hashlib.sha256(encoded_chunk.encode()).hexdigest()[:12]
        fake_mac = ":".join(
            chunk_hash[i : i + 2] for i in range(0, 12, 2)
        )

        def _send():
            pkt = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(
                op="is-at",
                hwsrc=fake_mac,
                psrc="0.0.0.0",
                hwdst="ff:ff:ff:ff:ff:ff",
                pdst="255.255.255.255",
            )
            sendp(pkt, iface=self._interface, verbose=False)

        await asyncio.to_thread(_send)
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="arp", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not in ARP cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"arp://{self._interface}/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name="arp", location=loc)
            for loc in self._cache
            if loc.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.close()
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
