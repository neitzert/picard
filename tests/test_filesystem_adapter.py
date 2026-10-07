"""Tests for the filesystem adapter -- uses real filesystem via tmp_path."""

from __future__ import annotations

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.filesystem import FilesystemAdapter
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
class TestFilesystemAdapter:
    async def test_profile(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        p = a.profile()
        assert p.name == "filesystem"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is True

    async def test_write_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "filesystem"
        assert ref.location.endswith(".pchk")

    async def test_write_and_read_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_missing_file_raises(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        ref = StoredChunkRef(adapter_name="filesystem", location=str(tmp_path / "nonexistent.pchk"))
        with pytest.raises(FileNotFoundError):
            await a.read_chunk(ref)

    async def test_delete_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        assert await a.delete_chunk(ref) is False

    async def test_delete_missing(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        ref = StoredChunkRef(adapter_name="filesystem", location=str(tmp_path / "no.pchk"))
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        await a.write_chunk(enc1)
        await a.write_chunk(enc2)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "filesystem" for r in refs)

    async def test_list_chunks_empty(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        assert await a.health_check() is True

    async def test_capacity(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None and cap["used_bytes"] > 0
        assert cap["free_bytes"] is not None
        assert cap["total_bytes"] is not None

    async def test_capacity_empty(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        cap = await a.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_chunk_path_format(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        p = a._chunk_path("m1", 0, 2)
        assert p.name == "b0_c2.pchk"
        assert p.parent.name == "m1"

    async def test_multiple_manifests(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m2", 0, 0)
        await a.write_chunk(enc1)
        await a.write_chunk(enc2)

        refs_m1 = await a.list_chunks("m1")
        refs_m2 = await a.list_chunks("m2")
        assert len(refs_m1) == 1
        assert len(refs_m2) == 1
