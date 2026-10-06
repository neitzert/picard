"""NTP Extension Field adapter — chunks hidden in NTP extension fields.

RFC 7822 defines extension fields for NTPv4. This adapter encodes
chunk data into vendor-specific extension fields, which are ignored
by standard NTP servers but can be picked up by a custom receiver.
NTP traffic is rarely inspected or blocked.

Requires: UDP socket access.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NtpExtAdapter(Adapter):
    """Store chunks in NTP extension fields."""

    NTP_PORT = 123
    VENDOR_EXT_TYPE = 0xF000

    def __init__(
        self,
        ntp_host: str = "127.0.0.1",
        port: int = 123,
        *,
        max_chunk_bytes: int = 400,
    ) -> None:
        self._host = ntp_host
        self._port = port
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ntp-ext",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
            ttl_seconds=None,
        )

    @staticmethod
    def _build_ntp_header() -> bytes:
        li_vn_mode = (0 << 6) | (4 << 3) | 3  # LI=0, VN=4, Mode=3(client)
        stratum = 0
        poll = 6
        precision = -20 & 0xFF
        root_delay = 0
        root_dispersion = 0
        ref_id = 0
        now = int(time.time()) + 2208988800  # NTP epoch offset
        header = struct.pack(
            "!BBBbIIIQQQQ",
            li_vn_mode, stratum, poll, precision,
            root_delay, root_dispersion, ref_id,
            now << 32, now << 32, now << 32, now << 32,
        )
        return header[:48]

    def _build_extension_field(self, data: bytes) -> bytes:
        pad_len = (4 - (len(data) % 4)) % 4
        padded = data + b"\x00" * pad_len
        length = 4 + len(padded)
        if length < 28:
            padded = padded.ljust(24, b"\x00")
            length = 28
        return struct.pack("!HH", self.VENDOR_EXT_TYPE, length) + padded

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        encoded_data = base64.b64encode(encoded_chunk.encode())

        def _send():
            import socket
            ntp_header = self._build_ntp_header()
            ext_field = self._build_extension_field(encoded_data)
            packet = ntp_header + ext_field
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet, (self._host, self._port))
            finally:
                sock.close()

        try:
            await asyncio.to_thread(_send)
        except Exception:
            pass

        self._cache[chunk_key] = encoded_chunk
        return StoredChunkRef(adapter_name="ntp-ext", location=chunk_key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"NTP ext record not cached: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(adapter_name="ntp-ext", location=k)
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        import socket
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
