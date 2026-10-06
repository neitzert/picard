"""Tests for the named pipe adapter."""

import pytest

from picard.adapters.named_pipe import NamedPipeAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter(tmp_path):
    return NamedPipeAdapter(pipe_dir=tmp_path / "pipes", max_chunk_bytes=4096)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "named-pipe"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("pipe-data")
        assert ref.adapter_name == "named-pipe"
        assert "pipe://" in ref.location

    async def test_round_trip(self, adapter):
        data = "round-trip-pipe"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_read_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="named-pipe", location="pipe:///tmp/pipes/nonexistent")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="named-pipe", location="pipe:///tmp/pipes/nope")
        assert await adapter.delete_chunk(ref) is False

    async def test_read_after_delete(self, adapter):
        ref = await adapter.write_chunk("gone")
        await adapter.delete_chunk(ref)
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        refs = await adapter.list_chunks("nonexistent")
        assert refs == []


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_capacity_with_data(self, adapter):
        await adapter.write_chunk("some data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
