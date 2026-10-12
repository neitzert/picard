"""PROXY Protocol v2 adapter -- stores chunks in PP2 TLV extensions.

PROXY Protocol v2 (used by HAProxy, AWS NLB, Cloudflare, etc.)
supports custom TLV (type-length-value) fields after the header.
Chunks are embedded in custom TLV types (0xE0-0xEF, reserved for
private use) and sent over TCP connections.

Requires: no external dependencies (uses stdlib socket + struct).
"""

from __future__ import annotations

import asyncio
import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# PROXY Protocol v2 signature
_PP2_SIG = b"\x0d\x0a\x0d\x0a\x00\x0d\x0a\x51\x55\x49\x54\x0a"
_PP2_VERSION_COMMAND = 0x21  # version 2, PROXY command
_PP2_FAMILY_TCP4 = 0x11  # AF_INET, STREAM
_PP2_TLV_TYPE_CUSTOM = 0xE0  # private-use TLV range


class ProxyProtocolAdapter(Adapter):
    """Scatter chunks via PROXY Protocol v2 TLV fields."""

    def __init__(
        self,
        dest_host: str,
        dest_port: int,
        *,
        src_host: str = "127.0.0.1",
        src_port: int = 0,
        tlv_type: int = _PP2_TLV_TYPE_CUSTOM,
        max_chunk_bytes: int = 65535,
    ) -> None:
        self._dest_host = dest_host
        self._dest_port = dest_port
        self._src_host = src_host
        self._src_port = src_port or 49152
        self._tlv_type = tlv_type
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="proxy-protocol",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=500,
        )

    def _build_pp2_header(self, tlv_data: bytes) -> bytes:
        """Build a PROXY Protocol v2 header with custom TLV."""
        src_addr = socket.inet_aton(self._src_host)
        dst_addr = socket.inet_aton(self._dest_host)
        addrs = src_addr + dst_addr + struct.pack("!HH", self._src_port, self._dest_port)
        tlv = struct.pack("!BH", self._tlv_type, len(tlv_data)) + tlv_data
        total_len = len(addrs) + len(tlv)
        header = (
            _PP2_SIG
            + struct.pack("!BB", _PP2_VERSION_COMMAND, _PP2_FAMILY_TCP4)
            + struct.pack("!H", total_len)
            + addrs
            + tlv
        )
        return header

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            return (
                f"pp2://{self._dest_host}:{self._dest_port}/"
                f"{self._tlv_type:02x}/"
                f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
            )
        except (ValueError, Exception):
            return (
                f"pp2://{self._dest_host}:{self._dest_port}/"
                f"{self._tlv_type:02x}/{uuid.uuid4().hex}"
            )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        loc = self._location_key(encoded_chunk)

        def _send():
            data = encoded_chunk.encode("utf-8")
            header = self._build_pp2_header(data)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(5)
                sock.connect((self._dest_host, self._dest_port))
                sock.sendall(header)
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="proxy-protocol", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not in PP2 cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        search = f"/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name="proxy-protocol", location=loc)
            for loc in self._cache
            if search in loc
        ]

    async def health_check(self) -> bool:
        def _check():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(3)
                sock.connect((self._dest_host, self._dest_port))
                return True
            except Exception:
                return False
            finally:
                sock.close()

        return await asyncio.to_thread(_check)

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
