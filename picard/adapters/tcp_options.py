"""TCP options covert channel adapter — binary protocol over TCP.

Embeds chunk data in a custom binary framing protocol over a standard
TCP connection to a cooperating server.  Inspired by TCP option field
abuse (RFC 6994 experimental kinds 253/254), but uses an application-
layer binary protocol for portability.

Wire format:
  Request:  length(4B BE) | op(1B) | msg_id(16B) | payload
  Response: length(4B BE) | status(1B, 0=OK 1=ERR) | payload

Op codes: PING=0x00, WRITE=0x01, READ=0x02, DELETE=0x03, LIST=0x04
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_OP_PING = 0x00
_OP_WRITE = 0x01
_OP_READ = 0x02
_OP_DELETE = 0x03
_OP_LIST = 0x04

_STATUS_OK = 0x00


class TcpOptionsAdapter(Adapter):
    """Covert channel over TCP using a custom binary protocol."""

    def __init__(
        self,
        host: str,
        port: int = 9998,
        *,
        max_chunk_bytes: int = 1200,
    ) -> None:
        self._host = host
        self._port = port
        self._max = max_chunk_bytes
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_connection(self):
        if self._reader is not None and self._writer is not None:
            return
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port,
        )

    async def _send(self, op: int, msg_id: bytes, payload: bytes = b"") -> None:
        body = struct.pack("B", op) + msg_id + payload
        frame = struct.pack(">I", len(body)) + body
        self._writer.write(frame)  # type: ignore[union-attr]
        await self._writer.drain()  # type: ignore[union-attr]

    async def _recv(self) -> tuple[int, bytes]:
        length_bytes = await self._reader.readexactly(4)  # type: ignore[union-attr]
        (length,) = struct.unpack(">I", length_bytes)
        body = await self._reader.readexactly(length)  # type: ignore[union-attr]
        status = body[0]
        payload = body[1:]
        return status, payload

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tcp-options",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_connection()
        msg_id = uuid.uuid4().bytes
        await self._send(_OP_WRITE, msg_id, encoded_chunk.encode("utf-8"))
        status, _ = await self._recv()
        if status != _STATUS_OK:
            raise RuntimeError("tcp-options write failed")
        return StoredChunkRef(
            adapter_name="tcp-options",
            location=msg_id.hex(),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        await self._ensure_connection()
        msg_id = bytes.fromhex(ref.location)
        await self._send(_OP_READ, msg_id)
        status, payload = await self._recv()
        if status != _STATUS_OK:
            raise RuntimeError(f"tcp-options read failed for {ref.location}")
        return payload.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        await self._ensure_connection()
        msg_id = bytes.fromhex(ref.location)
        try:
            await self._send(_OP_DELETE, msg_id)
            status, _ = await self._recv()
            return status == _STATUS_OK
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        await self._ensure_connection()
        await self._send(_OP_LIST, b"\x00" * 16, manifest_id.encode("utf-8"))
        status, payload = await self._recv()
        if status != _STATUS_OK or not payload:
            return []
        ids = payload.decode("utf-8").splitlines()
        return [
            StoredChunkRef(adapter_name="tcp-options", location=mid)
            for mid in ids
            if mid
        ]

    async def health_check(self) -> bool:
        try:
            await self._ensure_connection()
            await self._send(_OP_PING, b"\x00" * 16)
            status, _ = await self._recv()
            return status == _STATUS_OK
        except Exception:
            self._reader = None
            self._writer = None
            return False
