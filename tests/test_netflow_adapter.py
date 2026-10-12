"""Tests for NetFlow adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from picard.adapters.netflow_adapter import NetflowAdapter


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture
def adapter():
    return NetflowAdapter(collector_host="127.0.0.1", collector_port=2055)


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "netflow"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 1400

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False

    def test_profile_high_rate(self, adapter):
        p = adapter.profile()
        assert p.rate_limit_per_min == 600


class TestBuildHeader:
    def test_header_length(self, adapter):
        header = adapter._build_header(2)
        assert len(header) == 16

    def test_seq_increments(self, adapter):
        adapter._build_header(1)
        assert adapter._seq == 1
        adapter._build_header(1)
        assert adapter._seq == 2


class TestBuildTemplateFlowset:
    def test_flowset_starts_with_zero(self, adapter):
        fs = adapter._build_template_flowset()
        import struct
        flowset_id = struct.unpack("!H", fs[:2])[0]
        assert flowset_id == 0


class TestBuildDataFlowset:
    def test_data_flowset_has_template_id(self, adapter):
        fs = adapter._build_data_flowset("key", b"data")
        import struct
        flowset_id = struct.unpack("!H", fs[:2])[0]
        assert flowset_id == adapter.TEMPLATE_ID


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("test-data")
            assert ref.adapter_name == "netflow"
            assert ref.location in adapter._cache

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"hello")
            ref = await adapter.write_chunk(chunk.encode())
            assert ref.location == "m1_0_0"

    @pytest.mark.asyncio
    async def test_write_caches_data(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("my-payload")
            assert adapter._cache[ref.location] == "my-payload"


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("test-data")
            result = await adapter.read_chunk(ref)
            assert result == "test-data"

    @pytest.mark.asyncio
    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="netflow", location="missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("data")
            assert await adapter.delete_chunk(ref) is True

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="netflow", location="missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
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

    @pytest.mark.asyncio
    async def test_capacity_after_write(self, adapter):
        with patch("picard.adapters.netflow_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            await adapter.write_chunk("data")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
