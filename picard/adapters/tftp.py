"""TFTP adapter — stores chunks as files on a TFTP server.

Each chunk is a file named ``picard_{manifest}_{block}_{chunk}.pchk``.
Uses raw UDP per RFC 1350 — no external dependency.

Requires: a reachable TFTP server (no pip extra).
"""

from __future__ import annotations

import asyncio
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

OP_RRQ = 1
OP_WRQ = 2
OP_DATA = 3
OP_ACK = 4
OP_ERROR = 5

BLOCK_SIZE = 512


class TftpAdapter(Adapter):
    """Scatter chunks as files on a TFTP server."""

    def __init__(
        self,
        host: str,
        port: int = 69,
        *,
        max_chunk_bytes: int = 50_000,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._max = max_chunk_bytes
        self._timeout = timeout

    def _filename(self, manifest_id: str, block: int, chunk: int) -> str:
        return f"picard_{manifest_id}_b{block}_c{chunk}.pchk"

    @staticmethod
    def _build_rrq(filename: str) -> bytes:
        return struct.pack("!H", OP_RRQ) + filename.encode() + b"\x00" + b"octet\x00"

    @staticmethod
    def _build_wrq(filename: str) -> bytes:
        return struct.pack("!H", OP_WRQ) + filename.encode() + b"\x00" + b"octet\x00"

    @staticmethod
    def _build_data(block_num: int, data: bytes) -> bytes:
        return struct.pack("!HH", OP_DATA, block_num) + data

    @staticmethod
    def _build_ack(block_num: int) -> bytes:
        return struct.pack("!HH", OP_ACK, block_num)

    @staticmethod
    def _parse_response(data: bytes) -> tuple[int, bytes]:
        opcode = struct.unpack("!H", data[:2])[0]
        return opcode, data[2:]

    async def _send_recv(self, packet: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=(self._host, self._port),
        )
        try:
            future: asyncio.Future[bytes] = loop.create_future()

            class _Proto(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    if not future.done():
                        future.set_result(data)

                def error_received(self, exc):
                    if not future.done():
                        future.set_exception(exc)

            transport.close()
            transport, _ = await loop.create_datagram_endpoint(
                _Proto,
                remote_addr=(self._host, self._port),
            )
            transport.sendto(packet)
            return await asyncio.wait_for(future, timeout=self._timeout)
        finally:
            transport.close()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tftp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        filename = self._filename(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index,
        )
        data = encoded_chunk.encode("utf-8")

        resp = await self._send_recv(self._build_wrq(filename))
        opcode, payload = self._parse_response(resp)
        if opcode == OP_ERROR:
            raise IOError(f"TFTP error on WRQ: {payload[2:].decode(errors='replace')}")

        block_num = 1
        offset = 0
        while offset < len(data):
            block = data[offset : offset + BLOCK_SIZE]
            resp = await self._send_recv(self._build_data(block_num, block))
            opcode, _ = self._parse_response(resp)
            if opcode == OP_ERROR:
                raise IOError("TFTP error during DATA transfer")
            block_num += 1
            offset += BLOCK_SIZE

        if len(data) % BLOCK_SIZE == 0:
            await self._send_recv(self._build_data(block_num, b""))

        return StoredChunkRef(adapter_name="tftp", location=filename)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        resp = await self._send_recv(self._build_rrq(ref.location))
        opcode, payload = self._parse_response(resp)
        if opcode == OP_ERROR:
            raise IOError(
                f"TFTP error on RRQ: {payload[2:].decode(errors='replace')}"
            )

        parts: list[bytes] = []
        while True:
            block_num = struct.unpack("!H", payload[:2])[0]
            block_data = payload[2:]
            parts.append(block_data)
            if len(block_data) < BLOCK_SIZE:
                await self._send_recv(self._build_ack(block_num))
                break
            resp = await self._send_recv(self._build_ack(block_num))
            opcode, payload = self._parse_response(resp)
            if opcode == OP_ERROR:
                raise IOError("TFTP error during read")

        return b"".join(parts).decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            resp = await self._send_recv(self._build_wrq(ref.location))
            opcode, _ = self._parse_response(resp)
            if opcode == OP_ERROR:
                return False
            await self._send_recv(self._build_data(1, b""))
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            resp = await self._send_recv(
                self._build_rrq("__picard_healthcheck__")
            )
            opcode, _ = self._parse_response(resp)
            return opcode in (OP_DATA, OP_ERROR)
        except Exception:
            return False
