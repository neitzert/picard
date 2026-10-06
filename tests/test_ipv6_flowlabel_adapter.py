"""Tests for the IPv6 flow label adapter -- mocked, no network calls."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ipv6_flowlabel import (
    STATUS_OK,
    IPv6FlowLabelAdapter,
    _flow_label_for,
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


def _make_adapter(**kwargs) -> IPv6FlowLabelAdapter:
    defaults = dict(target_host="::1", target_port=9999)
    defaults.update(kwargs)
    return IPv6FlowLabelAdapter(**defaults)


def _ok_response(payload: bytes = b"") -> bytes:
    return struct.pack("!B", STATUS_OK) + payload


def _err_response() -> bytes:
    return struct.pack("!B", 0x01)


class TestFlowLabelHelper:
    def test_flow_label_20_bits(self):
        fl = _flow_label_for("test-id")
        assert 0 <= fl <= 0xFFFFF

    def test_flow_label_deterministic(self):
        assert _flow_label_for("abc") == _flow_label_for("abc")

    def test_flow_label_varies(self):
        assert _flow_label_for("abc") != _flow_label_for("xyz")


@pytest.mark.asyncio
class TestIPv6FlowLabelAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ipv6-flowlabel"
        assert p.max_chunk_bytes == 200
        assert p.persistent is False
        assert p.ordered is True
        assert p.rate_limit_per_min == 600

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())

        ref = await a.write_chunk(_sample_encoded())

        assert ref.adapter_name == "ipv6-flowlabel"
        assert len(ref.location) == 32
        a._send_recv.assert_awaited_once()
        _, kwargs = a._send_recv.call_args
        assert "flow_label" in kwargs
        assert 0 <= kwargs["flow_label"] <= 0xFFFFF

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        with pytest.raises(RuntimeError, match="write failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=_ok_response(enc.encode()))

        ref = StoredChunkRef(adapter_name="ipv6-flowlabel", location="ab" * 16)
        result = await a.read_chunk(ref)

        assert result == enc
        _, kwargs = a._send_recv.call_args
        expected_fl = _flow_label_for("ab" * 16)
        assert kwargs["flow_label"] == expected_fl

    async def test_read_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        ref = StoredChunkRef(adapter_name="ipv6-flowlabel", location="ab" * 16)
        with pytest.raises(RuntimeError, match="read failed"):
            await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())

        ref = StoredChunkRef(adapter_name="ipv6-flowlabel", location="cd" * 16)
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_err_response())

        ref = StoredChunkRef(adapter_name="ipv6-flowlabel", location="cd" * 16)
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("no route"))

        ref = StoredChunkRef(adapter_name="ipv6-flowlabel", location="cd" * 16)
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
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
