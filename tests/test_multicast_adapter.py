"""Tests for Multicast adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from picard.adapters.multicast_adapter import MulticastAdapter


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.fixture
def adapter():
    return MulticastAdapter(group="239.255.42.42", port=5007)


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "multicast"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 1400

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False

    def test_profile_high_rate(self, adapter):
        p = adapter.profile()
        assert p.rate_limit_per_min == 600


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("test-data")
            assert ref.adapter_name == "multicast"

    @pytest.mark.asyncio
    async def test_write_caches_data(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("payload")
            assert adapter._cache[ref.location] == "payload"

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"x")
            ref = await adapter.write_chunk(chunk.encode())
            assert ref.location == "m1_0_0"

    @pytest.mark.asyncio
    async def test_write_sends_json(self, adapter):
        mock_inst = MagicMock()
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=mock_inst):
            await adapter.write_chunk("test-data")
            mock_inst.sendto.assert_called_once()
            args = mock_inst.sendto.call_args
            import json
            msg = json.loads(args[0][0])
            assert "key" in msg
            assert "data" in msg


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("multicast-data")
            result = await adapter.read_chunk(ref)
            assert result == "multicast-data"

    @pytest.mark.asyncio
    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="multicast", location="missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            ref = await adapter.write_chunk("data")
            assert await adapter.delete_chunk(ref) is True

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="multicast", location="missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            from picard.chunker import Chunk
            c1 = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"a")
            c2 = Chunk(version=1, manifest_id="m2", block_index=0, chunk_index=0, total_chunks=1, payload=b"b")
            await adapter.write_chunk(c1.encode())
            await adapter.write_chunk(c2.encode())
            result = await adapter.list_chunks("m1")
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_empty(self, adapter):
        result = await adapter.list_chunks("nonexistent")
        assert result == []


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_failure(self, adapter):
        with patch("socket.socket", side_effect=Exception("no multicast")):
            assert await adapter.health_check() is False


class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    @pytest.mark.asyncio
    async def test_capacity_after_write(self, adapter):
        with patch("picard.adapters.multicast_adapter.asyncio.to_thread", _fake_to_thread), \
             patch("socket.socket", return_value=MagicMock()):
            await adapter.write_chunk("data")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
            assert cap["used_bytes"] > 0
