"""Tests for the NTP extension field adapter -- mocked, no network calls."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ntp import (
    NtpAdapter,
    _NTP_HEADER,
    _FIELD_TYPE,
    _OP_ACK_DELETE,
    _OP_ACK_LIST,
    _OP_ACK_READ,
    _OP_ACK_WRITE,
    _build_extension,
    _parse_extension,
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


def _make_adapter(**kwargs) -> NtpAdapter:
    defaults = dict(server_host="127.0.0.1", server_port=1123)
    defaults.update(kwargs)
    return NtpAdapter(**defaults)


def _ack_response(op: int, payload: bytes = b"") -> bytes:
    ext = _build_extension(op, payload)
    return _NTP_HEADER + ext


class TestExtensionFields:
    def test_build_parse_round_trip(self):
        ext = _build_extension(0x01, b"hello")
        op, payload = _parse_extension(ext)
        assert op == 0x01
        assert payload.rstrip(b"\x00") == b"hello"

    def test_parse_wrong_field_type(self):
        bad = struct.pack("!HH", 0xAAAA, 8) + b"\x01\x00\x00\x00"
        with pytest.raises(ValueError, match="unexpected field type"):
            _parse_extension(bad)

    def test_parse_too_short(self):
        with pytest.raises(ValueError, match="extension too short"):
            _parse_extension(b"\x00\x01")


@pytest.mark.asyncio
class TestNtpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ntp"
        assert p.max_chunk_bytes == 400
        assert p.persistent is False
        assert p.ttl_seconds == 3600

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_WRITE))
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ntp"
        assert len(ref.location) == 16  # 8 bytes hex
        a._send_recv.assert_awaited_once()

    async def test_write_chunk_bad_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_READ))

        with pytest.raises(RuntimeError, match="unexpected NTP response"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        payload = enc.encode()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_READ, payload))

        ref = StoredChunkRef(adapter_name="ntp", location="aa" * 8)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_bad_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_WRITE))

        ref = StoredChunkRef(adapter_name="ntp", location="bb" * 8)
        with pytest.raises(RuntimeError, match="unexpected NTP response"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_DELETE))

        ref = StoredChunkRef(adapter_name="ntp", location="cc" * 8)
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        ref = StoredChunkRef(adapter_name="ntp", location="dd" * 8)
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        id1 = bytes(range(8))
        id2 = bytes(range(8, 16))
        payload = id1 + id2
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_LIST, payload))

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert refs[0].location == id1.hex()
        assert refs[1].location == id2.hex()

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ack_response(_OP_ACK_LIST, b""))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("unreachable"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_NTP_HEADER)

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        assert await a.health_check() is False

    async def test_health_check_short_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"\x00" * 10)

        assert await a.health_check() is False
