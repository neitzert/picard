"""Tests for the DOCX metadata adapter -- uses real filesystem via tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.docx_metadata import DocxMetadataAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


@pytest.mark.asyncio
class TestDocxMetadataAdapter:
    async def test_profile(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        p = a.profile()
        assert p.name == "docx-metadata"
        assert p.max_chunk_bytes == 65_536
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 120

    async def test_profile_custom_max(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path, max_chunk_bytes=32_768)
        p = a.profile()
        assert p.max_chunk_bytes == 32_768

    async def test_write_chunk(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "docx-metadata"
        assert ref.location.startswith("docx://")
        assert ref.location.endswith(".docx")

    async def test_write_and_read_chunk(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_write_creates_valid_docx(self, tmp_path):
        import zipfile

        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        path_str = ref.location.split("://", 1)[1]
        assert zipfile.is_zipfile(path_str)

    async def test_delete_chunk(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        assert await a.delete_chunk(ref) is False

    async def test_delete_missing(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        ref = StoredChunkRef(
            adapter_name="docx-metadata",
            location=f"docx://{tmp_path / 'nonexistent.docx'}",
        )
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        await a.write_chunk(enc1)
        await a.write_chunk(enc2)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "docx-metadata" for r in refs)

    async def test_list_chunks_empty(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_dir_exists(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        assert await a.health_check() is True

    async def test_health_check_dir_not_created(self, tmp_path):
        new_dir = tmp_path / "subdir"
        a = DocxMetadataAdapter.__new__(DocxMetadataAdapter)
        a._dir = new_dir
        a._max = 65_536
        assert await a.health_check() is True

    async def test_capacity(self, tmp_path):
        a = DocxMetadataAdapter(output_dir=tmp_path)
        enc = _sample_encoded()
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None and cap["used_bytes"] > 0
        assert cap["free_bytes"] is not None
        assert cap["total_bytes"] is not None

    async def test_capacity_empty_no_dir(self, tmp_path):
        new_dir = tmp_path / "empty"
        a = DocxMetadataAdapter.__new__(DocxMetadataAdapter)
        a._dir = new_dir
        a._max = 65_536
        cap = await a.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
