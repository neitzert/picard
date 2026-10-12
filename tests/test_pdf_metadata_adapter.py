"""Tests for the PDF metadata adapter."""

from __future__ import annotations

import pytest

from picard.adapters.pdf_metadata import PdfMetadataAdapter


@pytest.fixture
def adapter(tmp_path):
    return PdfMetadataAdapter(output_dir=tmp_path)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "pdf-metadata"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536


@pytest.mark.asyncio
class TestWriteRead:
    async def test_round_trip(self, adapter):
        ref = await adapter.write_chunk("hello-pdf-data")
        result = await adapter.read_chunk(ref)
        assert result == "hello-pdf-data"

    async def test_round_trip_special_chars(self, adapter):
        data = "data(with)parens\\and\\backslashes"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_creates_pdf_file(self, adapter, tmp_path):
        ref = await adapter.write_chunk("file-check")
        path_str = ref.location.removeprefix("pdf://")
        from pathlib import Path
        assert Path(path_str).exists()
        content = Path(path_str).read_bytes()
        assert content.startswith(b"%PDF-1.4")

    async def test_location_format(self, adapter):
        ref = await adapter.write_chunk("loc-check")
        assert ref.location.startswith("pdf://")
        assert ref.location.endswith(".pdf")


@pytest.mark.asyncio
class TestDelete:
    async def test_delete(self, adapter):
        ref = await adapter.write_chunk("del-me")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False

    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="pdf-metadata", location="pdf:///nonexistent.pdf")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    async def test_list_chunks(self, adapter):
        await adapter.write_chunk("PCHK:1:testman:0:0:1:ZGF0YTE=")
        refs = await adapter.list_chunks("testman")
        assert len(refs) >= 1


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["free_bytes"] is not None

    async def test_after_write(self, adapter):
        await adapter.write_chunk("cap-data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True


@pytest.mark.asyncio
class TestPdfGeneration:
    async def test_pdf_has_info_dict(self, adapter):
        ref = await adapter.write_chunk("info-check")
        from pathlib import Path
        content = Path(ref.location.removeprefix("pdf://")).read_bytes()
        text = content.decode("latin-1")
        assert "/Creator (picard)" in text
        assert "/Subject (" in text
