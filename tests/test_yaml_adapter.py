"""Tests for the YAML adapter."""

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.yaml_adapter import YAMLAdapter


@pytest.fixture
def adapter():
    return YAMLAdapter()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "yaml"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8192

    def test_ordered(self, adapter):
        assert adapter.profile().ordered is True


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("yaml chunk data")
        assert ref.adapter_name == "yaml"
        assert "yaml://" in ref.location

    async def test_write_caches(self, adapter):
        ref = await adapter.write_chunk("cached yaml")
        assert await adapter.read_chunk(ref) == "cached yaml"

    async def test_write_multiple(self, adapter):
        ref1 = await adapter.write_chunk("first")
        ref2 = await adapter.write_chunk("second")
        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        ref = await adapter.write_chunk("hello yaml")
        assert await adapter.read_chunk(ref) == "hello yaml"

    async def test_read_not_found(self, adapter):
        ref = StoredChunkRef(adapter_name="yaml", location="yaml://chunks/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(adapter_name="yaml", location="yaml://chunks/nope")
        assert await adapter.delete_chunk(ref) is False

    async def test_read_after_delete(self, adapter):
        ref = await adapter.write_chunk("gone")
        await adapter.delete_chunk(ref)
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestListChunks:
    async def test_empty(self, adapter):
        chunks = await adapter.list_chunks("any")
        assert len(chunks) == 0

    async def test_after_write(self, adapter):
        await adapter.write_chunk("a")
        await adapter.write_chunk("b")
        chunks = await adapter.list_chunks("any")
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


class TestBuildYAML:
    def test_valid_yaml_structure(self, adapter):
        import asyncio
        asyncio.get_event_loop().run_until_complete(adapter.write_chunk("yaml val"))
        yml = adapter._build_yaml()
        assert "picard:" in yml
        assert "chunks:" in yml
        assert "yaml val" in yml
