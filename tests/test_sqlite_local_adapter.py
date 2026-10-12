"""Tests for the local SQLite adapter."""

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sqlite_local import SQLiteLocalAdapter


@pytest.fixture
def adapter():
    return SQLiteLocalAdapter(":memory:")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "sqlite_local"

    def test_persistent_memory(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_ordered(self, adapter):
        assert adapter.profile().ordered is True


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("sqlite data")
        assert ref.adapter_name == "sqlite_local"
        assert "sqlite://" in ref.location

    async def test_write_and_read(self, adapter):
        ref = await adapter.write_chunk("roundtrip")
        assert await adapter.read_chunk(ref) == "roundtrip"

    async def test_write_multiple(self, adapter):
        ref1 = await adapter.write_chunk("first")
        ref2 = await adapter.write_chunk("second")
        assert ref1.location != ref2.location
        assert await adapter.read_chunk(ref1) == "first"
        assert await adapter.read_chunk(ref2) == "second"


@pytest.mark.asyncio
class TestRead:
    async def test_read_existing(self, adapter):
        ref = await adapter.write_chunk("hello sqlite")
        assert await adapter.read_chunk(ref) == "hello sqlite"

    async def test_read_not_found(self, adapter):
        ref = StoredChunkRef(
            adapter_name="sqlite_local",
            location="sqlite://:memory:/missing",
        )
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(
            adapter_name="sqlite_local",
            location="sqlite://:memory:/nope",
        )
        assert await adapter.delete_chunk(ref) is False

    async def test_read_after_delete(self, adapter):
        ref = await adapter.write_chunk("gone")
        await adapter.delete_chunk(ref)
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestListChunks:
    async def test_empty(self, adapter):
        chunks = await adapter.list_chunks("")
        assert len(chunks) == 0

    async def test_after_write(self, adapter):
        await adapter.write_chunk("a")
        await adapter.write_chunk("b")
        # Chunks have empty manifest_id by default
        chunks = await adapter.list_chunks("")
        assert len(chunks) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_after_write(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
