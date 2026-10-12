"""IPv6 flow label steganographic transport adapter.

Encodes a 20-bit correlation ID in the IPv6 flow label field for
out-of-band packet correlation.  Chunk data travels in the UDP payload
alongside control info (op, msg_id).  A cooperating receiver correlates
packets by flow label + source address.

No external dependencies — uses asyncio + socket stdlib.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
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


def _flow_label_for(msg_id: str) -> int:
    """Derive a 20-bit flow label from a message ID."""
    h = hashlib.sha256(msg_id.encode()).digest()
    return int.from_bytes(h[:3], "big") & 0xFFFFF


class IPv6FlowLabelAdapter(Adapter):
    """Steganographic transport using IPv6 flow labels for correlation."""

    def __init__(
        self,
        target_host: str,
        target_port: int = 9999,
        *,
        max_chunk_bytes: int = 200,
        timeout: float = 5.0,
    ) -> None:
        self._host = target_host
        self._port = target_port
        self._max = max_chunk_bytes
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ipv6-flowlabel",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
            rate_limit_per_min=600,
            ttl_seconds=None,
        )

    async def _send_recv(self, data: bytes, flow_label: int = 0) -> bytes:
        loop = asyncio.get_running_loop()

        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setblocking(False)

        if flow_label:
            tclass_flow = (flow_label & 0xFFFFF)
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_FLOWINFO, tclass_flow)
            except (OSError, AttributeError):
                pass

        try:
            await loop.sock_connect(sock, (self._host, self._port, 0, 0))
            await loop.sock_sendall(sock, data)
            return await asyncio.wait_for(loop.sock_recv(sock, 65535), timeout=self._timeout)
        finally:
            sock.close()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex
        fl = _flow_label_for(msg_id)
        packet = struct.pack("!B", OP_WRITE) + bytes.fromhex(msg_id) + encoded_chunk.encode()
        resp = await self._send_recv(packet, flow_label=fl)
        if resp[0] != STATUS_OK:
            raise RuntimeError("IPv6 flow label write failed")
        return StoredChunkRef(adapter_name="ipv6-flowlabel", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        fl = _flow_label_for(ref.location)
        packet = struct.pack("!B", OP_READ) + bytes.fromhex(ref.location)
        resp = await self._send_recv(packet, flow_label=fl)
        if resp[0] != STATUS_OK:
            raise RuntimeError("IPv6 flow label read failed")
        return resp[1:].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        fl = _flow_label_for(ref.location)
        packet = struct.pack("!B", OP_DELETE) + bytes.fromhex(ref.location)
        try:
            resp = await self._send_recv(packet, flow_label=fl)
            return resp[0] == STATUS_OK
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            resp = await self._send_recv(struct.pack("!B", OP_PING), flow_label=0)
            return resp[0] == STATUS_OK
        except Exception:
            return False
