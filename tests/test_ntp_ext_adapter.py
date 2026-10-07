"""Tests for NTP Extension Field adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from picard.adapters.ntp_ext_adapter import NtpExtAdapter


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture
def adapter():
    return NtpExtAdapter(ntp_host="127.0.0.1", port=123)


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "ntp-ext"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 400

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False


class TestBuildNtpHeader:
    def test_header_is_48_bytes(self):
        header = NtpExtAdapter._build_ntp_header()
        assert len(header) == 48

    def test_header_version_4(self):
        header = NtpExtAdapter._build_ntp_header()
        li_vn_mode = header[0]
        version = (li_vn_mode >> 3) & 0x7
        assert version == 4


class TestBuildExtensionField:
    def test_ext_field_min_length(self, adapter):
        result = adapter._build_extension_field(b"x")
        import struct
        ext_type, length = struct.unpack("!HH", result[:4])
        assert ext_type == adapter.VENDOR_EXT_TYPE
        assert length >= 28

    def test_ext_field_alignment(self, adapter):
        result = adapter._build_extension_field(b"hello")
        assert len(result) % 4 == 0


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("test-data")
            assert ref.adapter_name == "ntp-ext"

    @pytest.mark.asyncio
    async def test_write_caches_data(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("payload")
            assert adapter._cache[ref.location] == "payload"

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"x")
            ref = await adapter.write_chunk(chunk.encode())
            assert ref.location == "m1_0_0"


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("ntp-secret")
            result = await adapter.read_chunk(ref)
            assert result == "ntp-secret"

    @pytest.mark.asyncio
    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="ntp-ext", location="missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("data")
            assert await adapter.delete_chunk(ref) is True

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="ntp-ext", location="missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters(self, adapter):
        with patch("picard.adapters.ntp_ext_adapter.asyncio.to_thread", _fake_to_thread), \
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
