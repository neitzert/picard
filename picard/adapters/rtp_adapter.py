"""RTP adapter -- stores chunks in Real-time Transport Protocol payloads.

Each chunk is sent as an RTP packet with a custom dynamic payload type.
The sequence number and SSRC tag the chunk for identification.
Read-back uses a local cache since RTP is a unidirectional stream
protocol.

Requires: no external dependencies (uses stdlib socket + struct).
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class RtpAdapter(Adapter):
    """Scatter chunks as RTP payload packets."""

    def __init__(
        self,
        dest_host: str,
        dest_port: int,
        *,
        src_port: int = 0,
        payload_type: int = 96,
        max_chunk_bytes: int = 1200,
    ) -> None:
        self._dest_host = dest_host
        self._dest_port = dest_port
        self._src_port = src_port
        self._payload_type = payload_type & 0x7F
        self._max = max_chunk_bytes
        self._ssrc = struct.unpack("!I", os.urandom(4))[0]
        self._seq = 0
        self._timestamp = 0
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="rtp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=3000,
        )

    def _build_rtp_packet(self, payload: bytes) -> bytes:
        """Build an RTP packet (RFC 3550) with the given payload."""
        # V=2, P=0, X=0, CC=0 => 0x80
        byte0 = 0x80
        # M=0, PT=payload_type
        byte1 = self._payload_type
        seq = self._seq & 0xFFFF
        header = struct.pack(
            "!BBHII", byte0, byte1, seq, self._timestamp, self._ssrc
        )
        return header + payload

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            return (
                f"rtp://{self._dest_host}:{self._dest_port}/"
                f"{self._ssrc:08x}/"
                f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
            )
        except (ValueError, Exception):
            return (
                f"rtp://{self._dest_host}:{self._dest_port}/"
                f"{self._ssrc:08x}/{uuid.uuid4().hex}"
            )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        loc = self._location_key(encoded_chunk)

        def _send():
            payload = encoded_chunk.encode("utf-8")
            pkt = self._build_rtp_packet(payload)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if self._src_port:
                    sock.bind(("", self._src_port))
                sock.sendto(pkt, (self._dest_host, self._dest_port))
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        self._seq += 1
        self._timestamp += 160
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="rtp", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not in RTP cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        search = f"/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name="rtp", location=loc)
            for loc in self._cache
            if search in loc
        ]

    async def health_check(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
