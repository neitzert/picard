"""Tests for SNMP Trap adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from picard.adapters.snmp_trap_adapter import SnmpTrapAdapter


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture
def adapter():
    return SnmpTrapAdapter(trap_receiver="127.0.0.1", port=162)


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "snmp-trap"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 1400

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False


class TestBerEncoding:
    def test_encode_length_short(self):
        assert SnmpTrapAdapter._encode_length(10) == b"\x0a"

    def test_encode_length_one_byte(self):
        assert SnmpTrapAdapter._encode_length(200) == b"\x81\xc8"

    def test_encode_length_two_byte(self):
        result = SnmpTrapAdapter._encode_length(300)
        assert result == b"\x82\x01\x2c"

    def test_encode_oid(self):
        result = SnmpTrapAdapter._encode_oid("1.3.6.1")
        assert result[0:1] == b"\x06"
        assert len(result) > 2

    def test_encode_octet_string(self):
        result = SnmpTrapAdapter._encode_octet_string(b"hello")
        assert result[0:1] == b"\x04"
        assert b"hello" in result

    def test_encode_integer_zero(self):
        result = SnmpTrapAdapter._encode_integer(0)
        assert result == b"\x02\x01\x00"

    def test_encode_integer_positive(self):
        result = SnmpTrapAdapter._encode_integer(42)
        assert result[0:1] == b"\x02"


class TestBuildTrap:
    def test_trap_is_bytes(self, adapter):
        result = adapter._build_trap("key1", "data1")
        assert isinstance(result, bytes)

    def test_trap_starts_with_sequence(self, adapter):
        result = adapter._build_trap("key1", "data1")
        assert result[0:1] == b"\x30"


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("test-data")
            assert ref.adapter_name == "snmp-trap"

    @pytest.mark.asyncio
    async def test_write_caches_data(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("payload")
            assert adapter._cache[ref.location] == "payload"

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"x")
            ref = await adapter.write_chunk(chunk.encode())
            assert ref.location == "m1_0_0"


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("secret")
            result = await adapter.read_chunk(ref)
            assert result == "secret"

    @pytest.mark.asyncio
    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="snmp-trap", location="missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("data")
            assert await adapter.delete_chunk(ref) is True

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="snmp-trap", location="missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters(self, adapter):
        with patch("picard.adapters.snmp_trap_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            c1 = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"a")
            c2 = Chunk(version=1, manifest_id="m2", block_index=0, chunk_index=0, total_chunks=1, payload=b"b")
            await adapter.write_chunk(c1.encode())
            await adapter.write_chunk(c2.encode())
            result = await adapter.list_chunks("m1")
            assert len(result) == 1


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
