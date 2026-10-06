"""UDP datagram adapter — stores chunks via a cooperating UDP receiver.

Each chunk is sent as a raw UDP datagram with a simple binary header.
The cooperating receiver maintains a key-value store indexed by message
IDs.  Fast, connectionless, no handshake.

No external dependencies — uses asyncio stdlib.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

OP_PING = 0x00
OP_WRITE = 0x01
OP_READ = 0x02
OP_DELETE = 0x03
OP_LIST = 0x04

STATUS_OK = 0x00
STATUS_ERROR = 0x01


class UdpAdapter(Adapter):
    """Scatter chunks as UDP datagrams to a cooperating receiver."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        max_chunk_bytes: int = 1400,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._max = max_chunk_bytes
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="udp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            ttl_seconds=None,
        )

    async def _send_recv(self, data: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        class _Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                if not fut.done():
                    fut.set_result(data)

            def error_received(self, exc: Exception) -> None:
                if not fut.done():
                    fut.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Protocol, remote_addr=(self._host, self._port),
        )
        try:
            transport.sendto(data)
            return await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().bytes
        packet = struct.pack("!B", OP_WRITE) + msg_id + encoded_chunk.encode()
        resp = await self._send_recv(packet)
        status = resp[0]
        if status != STATUS_OK:
            raise RuntimeError("UDP write failed")
        return StoredChunkRef(adapter_name="udp", location=msg_id.hex())

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg_id = bytes.fromhex(ref.location)
        packet = struct.pack("!B", OP_READ) + msg_id
        resp = await self._send_recv(packet)
        status = resp[0]
        if status != STATUS_OK:
            raise RuntimeError("UDP read failed")
        return resp[1:].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg_id = bytes.fromhex(ref.location)
        packet = struct.pack("!B", OP_DELETE) + msg_id
        try:
            resp = await self._send_recv(packet)
            return resp[0] == STATUS_OK
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        packet = struct.pack("!B", OP_LIST) + manifest_id.encode()
        try:
            resp = await self._send_recv(packet)
        except Exception:
            return []
        if resp[0] != STATUS_OK:
            return []
        payload = resp[1:].decode()
        if not payload.strip():
            return []
        refs: list[StoredChunkRef] = []
        for hex_id in payload.strip().split("\n"):
            refs.append(StoredChunkRef(adapter_name="udp", location=hex_id.strip()))
        return refs

    async def health_check(self) -> bool:
        try:
            resp = await self._send_recv(struct.pack("!B", OP_PING))
            return resp[0] == STATUS_OK
        except Exception:
            return False
