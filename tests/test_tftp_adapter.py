"""Tests for the TFTP adapter — mocked, no server needed."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tftp import (
    TftpAdapter,
    OP_ACK,
    OP_DATA,
    OP_ERROR,
    BLOCK_SIZE,
)
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> TftpAdapter:
    defaults = dict(host="192.168.1.1", port=69)
    defaults.update(kwargs)
    return TftpAdapter(**defaults)


def _ack_packet(block_num: int) -> bytes:
    return struct.pack("!HH", OP_ACK, block_num)


def _data_packet(block_num: int, data: bytes) -> bytes:
    return struct.pack("!HH", OP_DATA, block_num) + data


def _error_packet(code: int = 1, msg: str = "error") -> bytes:
    return struct.pack("!HH", OP_ERROR, code) + msg.encode() + b"\x00"


class TestTftpHelpers:
    def test_filename(self):
        a = _make_adapter()
        assert a._filename("m1", 0, 2) == "picard_m1_b0_c2.pchk"

    def test_build_rrq(self):
        pkt = TftpAdapter._build_rrq("test.txt")
        assert pkt[:2] == b"\x00\x01"
        assert b"test.txt" in pkt
        assert b"octet" in pkt

    def test_build_wrq(self):
        pkt = TftpAdapter._build_wrq("test.txt")
        assert pkt[:2] == b"\x00\x02"
        assert b"test.txt" in pkt

    def test_build_data(self):
        pkt = TftpAdapter._build_data(1, b"hello")
        op, blk = struct.unpack("!HH", pkt[:4])
        assert op == OP_DATA
        assert blk == 1
        assert pkt[4:] == b"hello"

    def test_build_ack(self):
        pkt = TftpAdapter._build_ack(3)
        op, blk = struct.unpack("!HH", pkt[:4])
        assert op == OP_ACK
        assert blk == 3

    def test_parse_response(self):
        pkt = _ack_packet(5)
        opcode, payload = TftpAdapter._parse_response(pkt)
        assert opcode == OP_ACK
        assert struct.unpack("!H", payload[:2])[0] == 5


@pytest.mark.asyncio
class TestTftpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "tftp"
        assert p.max_chunk_bytes == 50_000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        data_bytes = enc.encode("utf-8")

        responses = [_ack_packet(0)]
        block_count = (len(data_bytes) + BLOCK_SIZE - 1) // BLOCK_SIZE
        for i in range(1, block_count + 1):
            responses.append(_ack_packet(i))
        if len(data_bytes) % BLOCK_SIZE == 0:
            responses.append(_ack_packet(block_count + 1))

        a._send_recv = AsyncMock(side_effect=responses)

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "tftp"
        assert ref.location == "picard_m1_b0_c0.pchk"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        data_bytes = enc.encode("utf-8")

        data_resp = _data_packet(1, data_bytes)
        a._send_recv = AsyncMock(side_effect=[data_resp, _ack_packet(1)])

        ref = StoredChunkRef(adapter_name="tftp", location="picard_m1_b0_c0.pchk")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_packet(1, "File not found"))

        ref = StoredChunkRef(adapter_name="tftp", location="missing.pchk")
        with pytest.raises(IOError, match="TFTP error"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=[_ack_packet(0), _ack_packet(1)])

        ref = StoredChunkRef(adapter_name="tftp", location="picard_m1_b0_c0.pchk")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_packet(2, "Access denied"))

        ref = StoredChunkRef(adapter_name="tftp", location="picard_m1_b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        ref = StoredChunkRef(adapter_name="tftp", location="picard_m1_b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_packet(1, "not found"))
        assert await a.health_check() is True

    async def test_health_check_data_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_data_packet(1, b"surprise"))
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("unreachable"))
        assert await a.health_check() is False

    async def test_write_chunk_wrq_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_packet(2, "Disk full"))

        enc = _sample_encoded()
        with pytest.raises(IOError, match="TFTP error on WRQ"):
            await a.write_chunk(enc)
