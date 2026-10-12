"""DHCP adapter -- stores tiny chunks in DHCP vendor-specific options.

Uses DHCP option 224-254 (site-specific) or option 43 (vendor-specific)
to carry small payloads in DHCPINFORM messages.  Very low bandwidth.

Requires: pip install picard-store[dhcp]  (scapy)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class DhcpAdapter(Adapter):
    """Scatter tiny chunks via DHCP vendor-specific options."""

    def __init__(
        self,
        interface: str,
        *,
        server_ip: str | None = None,
        option_code: int = 224,
        max_chunk_bytes: int = 255,
    ) -> None:
        self._interface = interface
        self._server_ip = server_ip
        self._option_code = option_code
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dhcp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10,
        )

    def _build_inform(self, chunk_data: bytes, chunk_id: str):
        from scapy.all import DHCP, BOOTP, Ether, IP, UDP

        pkt = (
            Ether(dst="ff:ff:ff:ff:ff:ff")
            / IP(src="0.0.0.0", dst="255.255.255.255")
            / UDP(sport=68, dport=67)
            / BOOTP(op=1, xid=int(chunk_id[:8], 16) if len(chunk_id) >= 8 else 0)
            / DHCP(
                options=[
                    ("message-type", "inform"),
                    (self._option_code, chunk_data),
                    "end",
                ]
            )
        )
        return pkt

    def _send_packet(self, pkt) -> None:
        from scapy.all import sendp

        sendp(pkt, iface=self._interface, verbose=False)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_id = uuid.uuid4().hex

        chunk_bytes = encoded_chunk.encode("utf-8")
        pkt = self._build_inform(chunk_bytes, chunk_id)
        await asyncio.to_thread(self._send_packet, pkt)

        self._cache[chunk_id] = encoded_chunk
        location = f"dhcp://{self._interface}/{self._option_code}/{chunk_id}"
        return StoredChunkRef(adapter_name="dhcp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        val = self._cache.get(chunk_id)
        if val is None:
            raise KeyError(f"dhcp chunk not found: {chunk_id}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        return self._cache.pop(chunk_id, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(
                adapter_name="dhcp",
                location=f"dhcp://{self._interface}/{self._option_code}/{cid}",
            )
            for cid in self._cache
            if cid.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            import psutil

            ifaces = psutil.net_if_addrs()
            return self._interface in ifaces
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
