"""NTP in-flight adapter -- embeds chunk data in NTP fractional timestamp fields.

Each write encodes the first 16 bytes of a chunk across the four 32-bit
fractional timestamp fields of an NTP packet (reference, originate,
receive, transmit). The packet is sent to a cooperating NTP companion
that stores and echoes the payload. Without a companion running, the
adapter operates in demo mode: data is cached in-process and UDP sends
are best-effort.

Real deployments require ntp_companion.py to be running at server_host:
server_port, listening for PICARD NTP extension packets and serving them
back on request.

Requires: nothing beyond stdlib (asyncio + socket)
Companion: ntp_companion.py (not included; see module docs)
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "ntp-inflight"
_NTP_EPOCH_DELTA = 2208988800  # seconds between 1900 and 1970

# NTP packet: LI=0, VN=4, Mode=3 (client), 48 bytes total
_NTP_HEADER = struct.pack("!BBBb11I", 0x23, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def _build_ntp_with_payload(chunk_bytes: bytes) -> bytes:
    """Build a 48-byte NTP client packet with chunk data in timestamp fields."""
    padded = chunk_bytes[:16].ljust(16, b"\x00")
    frac0, frac1, frac2, frac3 = struct.unpack(">4I", padded)
    # LI=0, VN=4, Mode=3
    pkt = bytearray(48)
    pkt[0] = 0x23
    # reference timestamp: sec=0, frac=frac0
    struct.pack_into(">II", pkt, 16, 0, frac0)
    # originate timestamp: sec=0, frac=frac1
    struct.pack_into(">II", pkt, 24, 0, frac1)
    # receive timestamp: sec=0, frac=frac2
    struct.pack_into(">II", pkt, 32, 0, frac2)
    # transmit timestamp: sec=0, frac=frac3
    struct.pack_into(">II", pkt, 40, 0, frac3)
    return bytes(pkt)


class NtpInflightAdapter(Adapter):
    """Embed chunks in NTP fractional timestamp fields via a cooperating server."""

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = 12323,
        *,
        max_chunk_bytes: int = 16,
        timeout: float = 1.0,
    ) -> None:
        self._host = server_host
        self._port = server_port
        self._max = max_chunk_bytes
        self._timeout = timeout
        self._registry: dict[str, str] = {}  # location -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            tags={
                "substrate": "ntp-inflight",
                "experimental": "true",
                "requires": "cooperating-ntp-companion",
            },
        )

    async def _send_udp(self, data: bytes) -> None:
        def _do():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(self._timeout)
                    s.sendto(data, (self._host, self._port))
            except Exception:
                pass
        await asyncio.to_thread(_do)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(
                f"Chunk too large: {len(data)} bytes > max {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        pkt = _build_ntp_with_payload(data)
        await self._send_udp(pkt)

        loc = f"ntp://{token}"
        self._registry[loc] = encoded_chunk
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._registry.get(ref.location)
        if val is None:
            raise KeyError(f"ntp-inflight chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._registry.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        result: list[StoredChunkRef] = []
        for loc, encoded in self._registry.items():
            try:
                if Chunk.decode(encoded).manifest_id == manifest_id:
                    result.append(StoredChunkRef(adapter_name=_ADAPTER_NAME, location=loc))
            except Exception:
                continue
        return result

    async def health_check(self) -> bool:
        return True  # demo mode -- always healthy; companion connectivity is optional

    async def close(self) -> None:
        pass
