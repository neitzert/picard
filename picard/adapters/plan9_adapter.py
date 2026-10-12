"""9P adapter — stores chunks via Plan 9 file protocol.

Used by WSL2, QEMU virtio-9p, and Plan 9 systems:
    9p://{host}/{aname}/{manifest_id}/{block}_{chunk}

Requires: pip install picard-store[9p]
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

# 9P2000 message types
_TVERSION = 100
_RVERSION = 101
_TATTACH = 104
_RATTACH = 105
_TWALK = 110
_RWALK = 111
_TCREATE = 114
_RCREATE = 115
_TOPEN = 112
_ROPEN = 113
_TREAD = 116
_RREAD = 117
_TWRITE = 118
_RWRITE = 119
_TCLUNK = 120
_RCLUNK = 121
_TREMOVE = 122
_RREMOVE = 123


class Plan9Adapter(Adapter):
    """Scatter chunks via 9P2000 file protocol."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 564,
        aname: str = "picard",
        username: str = "none",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._host = host
        self._port = port
        self._aname = aname
        self._username = username
        self._max = max_chunk_bytes
        self._msize = 8192 + max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="9p",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _pack_string(self, s: str) -> bytes:
        encoded = s.encode("utf-8")
        return struct.pack("<H", len(encoded)) + encoded

    def _pack_msg(self, msg_type: int, tag: int, payload: bytes) -> bytes:
        size = 4 + 1 + 2 + len(payload)
        return struct.pack("<IBH", size, msg_type, tag) + payload

    async def _send_recv(self, reader, writer, msg_type: int, tag: int, payload: bytes) -> bytes:
        writer.write(self._pack_msg(msg_type, tag, payload))
        await writer.drain()
        header = await reader.readexactly(7)
        size, rtype, rtag = struct.unpack("<IBH", header)
        body = await reader.readexactly(size - 7)
        return body

    async def _connect(self):
        reader, writer = await asyncio.open_connection(self._host, self._port)
        # Tversion
        payload = struct.pack("<I", self._msize) + self._pack_string("9P2000")
        await self._send_recv(reader, writer, _TVERSION, 0xFFFF, payload)
        # Tattach
        payload = struct.pack("<II", 0, 0xFFFFFFFF) + self._pack_string(self._username) + self._pack_string(self._aname)
        await self._send_recv(reader, writer, _TATTACH, 1, payload)
        return reader, writer

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            filename = f"{chunk.manifest_id}.{chunk.block_index}.{chunk.chunk_index}"
        except ValueError:
            filename = uuid.uuid4().hex

        reader, writer = await self._connect()
        try:
            # Twalk to root
            payload = struct.pack("<IIH", 0, 1, 0)
            await self._send_recv(reader, writer, _TWALK, 2, payload)
            # Tcreate
            payload = struct.pack("<I", 1) + self._pack_string(filename) + struct.pack("<IB", 0o644, 0x02)
            await self._send_recv(reader, writer, _TCREATE, 3, payload)
            # Twrite
            data = encoded_chunk.encode()
            payload = struct.pack("<IQI", 1, 0, len(data)) + data
            await self._send_recv(reader, writer, _TWRITE, 4, payload)
            # Tclunk
            await self._send_recv(reader, writer, _TCLUNK, 5, struct.pack("<I", 1))
        finally:
            writer.close()
            await writer.wait_closed()

        location = f"9p://{self._host}/{self._aname}/{filename}"
        return StoredChunkRef(adapter_name="9p", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.split("/")
        filename = parts[-1]

        reader, writer = await self._connect()
        try:
            # Twalk to file
            payload = struct.pack("<IIH", 0, 1, 1) + self._pack_string(filename)
            await self._send_recv(reader, writer, _TWALK, 2, payload)
            # Topen
            payload = struct.pack("<IB", 1, 0x00)
            await self._send_recv(reader, writer, _TOPEN, 3, payload)
            # Tread
            payload = struct.pack("<IQI", 1, 0, self._max)
            body = await self._send_recv(reader, writer, _TREAD, 4, payload)
            count = struct.unpack("<I", body[:4])[0]
            data = body[4 : 4 + count]
            # Tclunk
            await self._send_recv(reader, writer, _TCLUNK, 5, struct.pack("<I", 1))
        finally:
            writer.close()
            await writer.wait_closed()
        return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.split("/")
        filename = parts[-1]

        try:
            reader, writer = await self._connect()
            try:
                payload = struct.pack("<IIH", 0, 1, 1) + self._pack_string(filename)
                await self._send_recv(reader, writer, _TWALK, 2, payload)
                await self._send_recv(reader, writer, _TREMOVE, 3, struct.pack("<I", 1))
            finally:
                writer.close()
                await writer.wait_closed()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            reader, writer = await self._connect()
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False
