"""Tests for the USB HID adapter -- mocked, no hardware required."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.usb_hid import (
    UsbHidAdapter,
    _HEADER_FMT,
    _HEADER_SIZE,
    _OP_ACK,
    _OP_DATA,
    _OP_DELETE,
    _OP_ERROR,
    _OP_LIST,
    _OP_READ,
    _OP_WRITE,
)
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"hid-test",
    ).encode()


def _make_adapter(**kwargs) -> UsbHidAdapter:
    return UsbHidAdapter(**kwargs)


def _ack_report(report_size: int = 64) -> bytes:
    data = struct.pack(_HEADER_FMT, 0x01, _OP_ACK, 0, b"\x00" * 8)
    return data.ljust(report_size, b"\x00")


def _data_report(seq: int, payload: bytes, report_size: int = 64) -> bytes:
    data = struct.pack(_HEADER_FMT, 0x01, _OP_DATA, seq, b"\x00" * 8) + payload
    return data.ljust(report_size, b"\x00")


def _error_report(report_size: int = 64) -> bytes:
    data = struct.pack(_HEADER_FMT, 0x01, _OP_ERROR, 0, b"\x00" * 8)
    return data.ljust(report_size, b"\x00")


@pytest.mark.asyncio
class TestUsbHidAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "usb-hid"
        assert p.max_chunk_bytes == 8000
        assert p.persistent is False
        assert p.ordered is True
        assert p.rate_limit_per_min == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_ack_report())
        a._make_msg_id = lambda: b"\x01\x02\x03\x04\x05\x06\x07\x08"

        ref = await a.write_chunk(_sample_encoded())

        assert ref.adapter_name == "usb-hid"
        assert ref.location == "0102030405060708"
        assert a._send_report.await_count >= 1

    async def test_write_chunk_error(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_error_report())
        a._make_msg_id = lambda: b"\x01" * 8

        with pytest.raises(IOError, match="error on write"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        enc = _sample_encoded()
        chunk_bytes = enc.encode("utf-8")

        a._recv_report = AsyncMock(
            side_effect=[
                _data_report(0, chunk_bytes),
                _ack_report(),
            ]
        )

        ref = StoredChunkRef(adapter_name="usb-hid", location="01" * 8)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_error(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_error_report())

        ref = StoredChunkRef(adapter_name="usb-hid", location="01" * 8)
        with pytest.raises(ValueError, match="device error"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_ack_report())

        ref = StoredChunkRef(adapter_name="usb-hid", location="01" * 8)
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_error_report())

        ref = StoredChunkRef(adapter_name="usb-hid", location="01" * 8)
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()

        entry = b"deadbeef01020304"
        a._recv_report = AsyncMock(
            side_effect=[
                _data_report(0, entry),
                _ack_report(),
            ]
        )

        refs = await a.list_chunks("m1")
        assert len(refs) == 1
        assert refs[0].location == "deadbeef01020304"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._send_report = AsyncMock()
        a._recv_report = AsyncMock(return_value=_ack_report())

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._open_device = AsyncMock(return_value="dev")
        a._close_device = AsyncMock()
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._open_device = AsyncMock(side_effect=Exception("no device"))
        assert await a.health_check() is False

    async def test_build_parse_roundtrip(self):
        a = _make_adapter()
        msg_id = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11"
        payload = b"test-data"
        report = a._build_report(0x01, _OP_WRITE, 7, msg_id, payload)
        assert len(report) == 64
        rid, op, seq, mid, ppay = a._parse_report(report)
        assert rid == 0x01
        assert op == _OP_WRITE
        assert seq == 7
        assert mid == msg_id
