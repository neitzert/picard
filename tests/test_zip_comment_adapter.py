"""Tests for the ZIP comment adapter."""

import zipfile

import pytest

from picard.adapters.zip_comment import ZipCommentAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter(tmp_path):
    return ZipCommentAdapter(output_dir=tmp_path / "zip_store", max_chunk_bytes=4096)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "zip-comment"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("zip-data")
        assert ref.adapter_name == "zip-comment"
        assert "zip://" in ref.location

    async def test_round_trip(self, adapter):
        data = "round-trip-zip-comment"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_zip_is_valid(self, adapter):
        ref = await adapter.write_chunk("valid-zip")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        assert zipfile.is_zipfile(Path(filename))

    async def test_data_in_comment(self, adapter):
        ref = await adapter.write_chunk("in-the-comment")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        with zipfile.ZipFile(Path(filename)) as zf:
            assert zf.comment == b"in-the-comment"

    async def test_zip_has_placeholder(self, adapter):
        ref = await adapter.write_chunk("placeholder-check")
        filename = ref.location.split("://", 1)[1]
        from pathlib import Path
        with zipfile.ZipFile(Path(filename)) as zf:
            assert "data.txt" in zf.namelist()


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("delete-me")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter, tmp_path):
        ref = StoredChunkRef(adapter_name="zip-comment", location=f"zip://{tmp_path}/nope.zip")
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
        assert cap["chunk_count"] is None or cap["chunk_count"] == 0

    async def test_capacity_with_data(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
