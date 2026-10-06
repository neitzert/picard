"""NTP extension field adapter — covert channel via NTP packets.

Embeds chunk data in RFC 7822 extension fields of NTP packets.
Requires a cooperating NTP server that understands the PICARD
extension field type (0xFF01) and stores/retrieves chunk payloads.

Uses only stdlib asyncio for raw UDP communication.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_FIELD_TYPE = 0xFF01

_OP_WRITE = 0x01
_OP_READ = 0x02
_OP_DELETE = 0x03
_OP_LIST = 0x04
_OP_ACK_WRITE = 0x81
_OP_ACK_READ = 0x82
_OP_ACK_DELETE = 0x83
_OP_ACK_LIST = 0x84

_NTP_HEADER = b"\x23" + b"\x00" * 47  # version 4, mode 3 (client), 48 bytes


def _build_extension(op: int, payload: bytes) -> bytes:
    body = struct.pack("!B", op) + payload
    padded_len = (len(body) + 4 + 3) & ~3
    body = body.ljust(padded_len - 4, b"\x00")
    return struct.pack("!HH", _FIELD_TYPE, padded_len) + body


def _parse_extension(data: bytes) -> tuple[int, bytes]:
    if len(data) < 5:
        raise ValueError("extension too short")
    field_type, length = struct.unpack("!HH", data[:4])
    if field_type != _FIELD_TYPE:
        raise ValueError(f"unexpected field type: 0x{field_type:04x}")
    op = data[4]
    payload = data[5:length]
    return op, payload


class NtpAdapter(Adapter):
    """Scatter chunks into NTP extension fields via a cooperating server."""

    def __init__(
        self,
        server_host: str,
        server_port: int = 123,
        *,
        max_chunk_bytes: int = 400,
        timeout: float = 5.0,
    ) -> None:
        self._host = server_host
        self._port = server_port
        self._max = max_chunk_bytes
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ntp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            ttl_seconds=3600,
        )

    async def _send_recv(self, data: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def __init__(self):
                self.transport = None

            def connection_made(self, transport):
                self.transport = transport
                transport.sendto(data)

            def datagram_received(self, recv_data, addr):
                if not fut.done():
                    fut.set_result(recv_data)

            def error_received(self, exc):
                if not fut.done():
                    fut.set_exception(exc)

        transport, _ = await loop.create_datagram_endpoint(
            _Proto, remote_addr=(self._host, self._port)
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            transport.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = os.urandom(8)
        ext = _build_extension(_OP_WRITE, msg_id + encoded_chunk.encode())
        packet = _NTP_HEADER + ext
        resp = await self._send_recv(packet)
        op, _ = _parse_extension(resp[48:])
        if op != _OP_ACK_WRITE:
            raise RuntimeError(f"unexpected NTP response op: 0x{op:02x}")
        return StoredChunkRef(adapter_name="ntp", location=msg_id.hex())

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg_id = bytes.fromhex(ref.location)
        ext = _build_extension(_OP_READ, msg_id)
        packet = _NTP_HEADER + ext
        resp = await self._send_recv(packet)
        op, payload = _parse_extension(resp[48:])
        if op != _OP_ACK_READ:
            raise RuntimeError(f"unexpected NTP response op: 0x{op:02x}")
        return payload.rstrip(b"\x00").decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg_id = bytes.fromhex(ref.location)
        ext = _build_extension(_OP_DELETE, msg_id)
        packet = _NTP_HEADER + ext
        try:
            resp = await self._send_recv(packet)
            op, _ = _parse_extension(resp[48:])
            return op == _OP_ACK_DELETE
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        ext = _build_extension(_OP_LIST, manifest_id.encode())
        packet = _NTP_HEADER + ext
        try:
            resp = await self._send_recv(packet)
            op, payload = _parse_extension(resp[48:])
            if op != _OP_ACK_LIST:
                return []
            payload = payload.rstrip(b"\x00")
            if not payload:
                return []
            refs: list[StoredChunkRef] = []
            for i in range(0, len(payload), 8):
                mid = payload[i : i + 8]
                if len(mid) == 8:
                    refs.append(
                        StoredChunkRef(adapter_name="ntp", location=mid.hex())
                    )
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            packet = _NTP_HEADER
            resp = await self._send_recv(packet)
            return len(resp) >= 48
        except Exception:
            return False
