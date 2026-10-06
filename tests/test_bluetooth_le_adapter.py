"""Tests for the Bluetooth LE adapter -- mocked, no hardware required."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.bluetooth_le import BluetoothLeAdapter, _COMPANY_ID, _OP_WRITE
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"ble-test",
    ).encode()


def _make_adapter(**kwargs) -> BluetoothLeAdapter:
    return BluetoothLeAdapter(**kwargs)


@pytest.mark.asyncio
class TestBluetoothLeAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "bluetooth-le"
        assert p.max_chunk_bytes == 20
        assert p.persistent is False
        assert p.ordered is False
        assert p.ttl_seconds == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        a._advertise = AsyncMock()
        a._make_msg_id = lambda: b"\x01\x02\x03\x04"
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "bluetooth-le"
        assert ref.location == "01020304"
        assert a._advertise.await_count > 0

    async def test_write_chunk_fragments(self):
        a = _make_adapter(max_chunk_bytes=20)
        a._advertise = AsyncMock()
        a._make_msg_id = lambda: b"\xaa\xbb\xcc\xdd"
        enc = _sample_encoded()

        await a.write_chunk(enc)

        assert a._advertise.await_count >= 1

    async def test_read_chunk(self):
        a = _make_adapter()
        a._advertise = AsyncMock()
        enc = _sample_encoded()
        chunk_bytes = enc.encode("utf-8")

        msg_id = b"\x01\x02\x03\x04"
        packet = a._build_packet(_OP_WRITE, msg_id, 0, chunk_bytes[:11])
        packet2 = a._build_packet(_OP_WRITE, msg_id, 1, chunk_bytes[11:])

        a._scan = AsyncMock(return_value=[packet, packet2])

        ref = StoredChunkRef(adapter_name="bluetooth-le", location="01020304")
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._stop_advertise = AsyncMock()

        ref = StoredChunkRef(adapter_name="bluetooth-le", location="01020304")
        assert await a.delete_chunk(ref) is True
        a._stop_advertise.assert_awaited_once()

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._stop_advertise = AsyncMock(side_effect=Exception("no adapter"))

        ref = StoredChunkRef(adapter_name="bluetooth-le", location="01020304")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc = _sample_encoded("m1")
        chunk_bytes = enc.encode("utf-8")
        msg_id = b"\x01\x02\x03\x04"
        packet = a._build_packet(_OP_WRITE, msg_id, 0, chunk_bytes)

        a._scan = AsyncMock(return_value=[packet])

        refs = await a.list_chunks("m1")
        assert len(refs) == 1
        assert refs[0].location == "01020304"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._scan = AsyncMock(return_value=[])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_scan_failure(self):
        a = _make_adapter()
        a._scan = AsyncMock(side_effect=Exception("no radio"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._scan = AsyncMock(return_value=[])
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._scan = AsyncMock(side_effect=Exception("no adapter"))
        assert await a.health_check() is False

    async def test_build_parse_roundtrip(self):
        a = _make_adapter()
        msg_id = b"\xde\xad\xbe\xef"
        payload = b"hello"
        packet = a._build_packet(0x01, msg_id, 42, payload)
        op, pid, seq, ppay = a._parse_packet(packet)
        assert op == 0x01
        assert pid == msg_id
        assert seq == 42
        assert ppay == payload
