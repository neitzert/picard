"""Tests for the filesystem adapter -- uses real I/O with tmp_path."""

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

    async def test_profile_custom_max(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path, max_chunk_bytes=8192)
        assert a.profile().max_chunk_bytes == 8192

    async def test_write_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "filesystem"
        assert "m1" in ref.location
        assert ref.location.endswith(".pchk")

    async def test_write_creates_file(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        from pathlib import Path
        assert Path(ref.location).is_file()

    async def test_read_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        ref = StoredChunkRef(adapter_name="filesystem", location=str(tmp_path / "nonexistent.pchk"))
        with pytest.raises(FileNotFoundError):
            await a.read_chunk(ref)

    async def test_delete_chunk(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        from pathlib import Path
        assert not Path(ref.location).exists()

    async def test_delete_chunk_not_found(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        ref = StoredChunkRef(adapter_name="filesystem", location=str(tmp_path / "nonexistent.pchk"))
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m1", 0, 1))
        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "filesystem" for r in refs)

    async def test_list_chunks_empty(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_list_chunks_isolation(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m2", 0, 0))
        refs_m1 = await a.list_chunks("m1")
        refs_m2 = await a.list_chunks("m2")
        assert len(refs_m1) == 1
        assert len(refs_m2) == 1

    async def test_health_check_ok(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        assert await a.health_check() is True

    async def test_capacity(self, tmp_path):
        a = FilesystemAdapter(root_dir=tmp_path)
        await a.write_chunk(_sample_encoded())
        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None and cap["used_bytes"] > 0
        assert cap["free_bytes"] is not None
        assert cap["total_bytes"] is not None
