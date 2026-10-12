"""STUN/TURN adapter -- stores chunks via TURN data relay channels.

Chunks are relayed through a TURN server's allocated channels.
Read-back depends on a local cache since TURN allocations are
ephemeral.  Useful in NAT-traversal scenarios where direct
connectivity is blocked.

Requires: pip install picard-store[stun]  (aioice or raw socket)
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_STUN_MAGIC = 0x2112A442
_STUN_BINDING_REQUEST = 0x0001
_STUN_HEADER_LEN = 20


class StunAdapter(Adapter):
    """Scatter chunks through STUN/TURN relay channels."""

    def __init__(
        self,
        server: str,
        *,
        port: int = 3478,
        username: str | None = None,
        credential: str | None = None,
        protocol: str = "udp",
        max_chunk_bytes: int = 1200,
    ) -> None:
        self._server = server
        self._port = port
        self._username = username
        self._credential = credential
        self._protocol = protocol
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._channel_counter = 0x4000

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="stun",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            return (
                f"turn://{self._server}/"
                f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
            )
        except (ValueError, Exception):
            return f"turn://{self._server}/{uuid.uuid4().hex}"

    def _build_stun_binding(self, txn_id: bytes) -> bytes:
        """Build a minimal STUN Binding Request."""
        msg_type = struct.pack("!H", _STUN_BINDING_REQUEST)
        msg_len = struct.pack("!H", 0)
        magic = struct.pack("!I", _STUN_MAGIC)
        return msg_type + msg_len + magic + txn_id

    def _build_channel_data(self, channel: int, data: bytes) -> bytes:
        """Build a TURN ChannelData message."""
        header = struct.pack("!HH", channel, len(data))
        padding = b"\x00" * ((4 - len(data) % 4) % 4)
        return header + data + padding

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        loc = self._location_key(encoded_chunk)
        channel = self._channel_counter
        self._channel_counter += 1

        def _send():
            data = encoded_chunk.encode("utf-8")
            msg = self._build_channel_data(channel, data)
            if self._protocol == "udp":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(5)
                if self._protocol == "tcp":
                    sock.connect((self._server, self._port))
                sock.sendto(msg, (self._server, self._port))
            finally:
                sock.close()

        await asyncio.to_thread(_send)
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="stun", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not in TURN cache: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"turn://{self._server}/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name="stun", location=loc)
            for loc in self._cache
            if loc.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        def _check():
            txn_id = hashlib.sha256(b"picard-health").digest()[:12]
            msg = self._build_stun_binding(txn_id)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.settimeout(3)
                sock.sendto(msg, (self._server, self._port))
                sock.recvfrom(1024)
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
