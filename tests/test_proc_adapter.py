"""Tests for the procfs / environment variable adapter."""

import os

import pytest

from picard.adapters.proc_adapter import ProcAdapter, _VAR_PREFIX
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter():
    return ProcAdapter(max_chunk_bytes=4096)


@pytest.fixture(autouse=True)
def cleanup_env():
    yield
    for key in list(os.environ):
        if key.startswith(_VAR_PREFIX):
            del os.environ[key]


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "procfs"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("env-data")
        assert ref.adapter_name == "procfs"
        assert "proc://" in ref.location

    async def test_round_trip(self, adapter):
        data = "round-trip-env"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_data_in_environ(self, adapter):
        await adapter.write_chunk("check-env")
        found = any(v == "check-env" for k, v in os.environ.items() if k.startswith(_VAR_PREFIX))
        assert found

    async def test_read_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="procfs", location="proc://self/environ/nonexistent")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="procfs", location="proc://self/environ/nope")
        assert await adapter.delete_chunk(ref) is False


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

    async def test_capacity_with_data(self, adapter):
        await adapter.write_chunk("some env data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
