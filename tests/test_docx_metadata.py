"""Tests for the DOCX metadata adapter."""

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.docx_metadata import DocxMetadataAdapter


@pytest.fixture
def adapter(tmp_path):
    return DocxMetadataAdapter(tmp_path / "docx_chunks")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "docx-metadata"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65_536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 120


@pytest.mark.asyncio
class TestRoundtrip:
    async def test_write_then_read(self, adapter):
        ref = await adapter.write_chunk("test chunk data")
        assert ref.adapter_name == "docx-metadata"
        assert ref.location.startswith("docx://")

        result = await adapter.read_chunk(ref)
        assert result == "test chunk data"

    async def test_write_creates_docx(self, adapter, tmp_path):
        ref = await adapter.write_chunk("data")
        path = ref.location.split("://", 1)[1]
        assert path.endswith(".docx")

    async def test_roundtrip_large_data(self, adapter):
        data = "A" * 50_000
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        ref = await adapter.write_chunk("data")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="docx-metadata", location="docx://nonexistent.docx")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        result = await adapter.list_chunks("no-such-manifest")
        assert result == []


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0

    async def test_capacity_with_data(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["used_bytes"] > 0
        assert cap["chunk_count"] == 1
