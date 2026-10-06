"""Tests for the UDP adapter -- mocked, no network calls."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.udp import (
    OP_DELETE,
    OP_LIST,
    OP_PING,
    OP_READ,
    OP_WRITE,
    STATUS_ERROR,
    STATUS_OK,
    UdpAdapter,
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


def _make_adapter(**kwargs) -> UdpAdapter:
    defaults = dict(host="127.0.0.1", port=9000)
    defaults.update(kwargs)
    return UdpAdapter(**defaults)


def _ok_response(payload: bytes = b"") -> bytes:
    return struct.pack("!B", STATUS_OK) + payload


def _err_response() -> bytes:
    return struct.pack("!B", STATUS_ERROR)


@pytest.mark.asyncio
class TestUdpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "udp"
        assert p.max_chunk_bytes == 1400
        assert p.persistent is False
        assert p.ordered is False
        assert p.rate_limit_per_min == 1000

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())

        ref = await a.write_chunk(_sample_encoded())

        assert ref.adapter_name == "udp"
        assert len(ref.location) == 32  # uuid hex
        call_data = a._send_recv.call_args[0][0]
        assert call_data[0] == OP_WRITE

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        with pytest.raises(RuntimeError, match="write failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=_ok_response(enc.encode()))

        ref = StoredChunkRef(adapter_name="udp", location="a" * 32)
        result = await a.read_chunk(ref)

        assert result == enc
        call_data = a._send_recv.call_args[0][0]
        assert call_data[0] == OP_READ

    async def test_read_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        ref = StoredChunkRef(adapter_name="udp", location="a" * 32)
        with pytest.raises(RuntimeError, match="read failed"):
            await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())

        ref = StoredChunkRef(adapter_name="udp", location="b" * 32)
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        ref = StoredChunkRef(adapter_name="udp", location="b" * 32)
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        ref = StoredChunkRef(adapter_name="udp", location="b" * 32)
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        ids = "aa" * 16 + "\n" + "bb" * 16
        a._send_recv = AsyncMock(return_value=_ok_response(ids.encode()))

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "udp" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response(b""))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("unreachable"))
        assert await a.health_check() is False
