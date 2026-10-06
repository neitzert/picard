"""Tests for the iCalendar adapter."""

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ical_adapter import ICalAdapter


@pytest.fixture
def adapter():
    return ICalAdapter("test-calendar")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "ical"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_ordered(self, adapter):
        assert adapter.profile().ordered is True


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("ical chunk data")
        assert ref.adapter_name == "ical"
        assert "ical://" in ref.location

    async def test_write_caches(self, adapter):
        ref = await adapter.write_chunk("cached ical")
        assert await adapter.read_chunk(ref) == "cached ical"

    async def test_write_multiple(self, adapter):
        ref1 = await adapter.write_chunk("event1")
        ref2 = await adapter.write_chunk("event2")
        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        ref = await adapter.write_chunk("hello ical")
        assert await adapter.read_chunk(ref) == "hello ical"

    async def test_read_not_found(self, adapter):
        ref = StoredChunkRef(adapter_name="ical", location="ical://test/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(adapter_name="ical", location="ical://test/nope")
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


class TestBuildICS:
    def test_valid_ics_structure(self, adapter):
        import asyncio
        asyncio.get_event_loop().run_until_complete(adapter.write_chunk("event data"))
        ics = adapter._build_ics()
        assert "BEGIN:VCALENDAR" in ics
        assert "END:VCALENDAR" in ics
        assert "BEGIN:VEVENT" in ics
        assert "END:VEVENT" in ics
        assert "DESCRIPTION:event data" in ics
        assert "PICARD:" in ics
