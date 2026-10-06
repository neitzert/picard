"""Tests for the memory and filesystem adapters."""

import tempfile

import pytest

from picard.adapters.filesystem import FilesystemAdapter
from picard.adapters.memory import MemoryAdapter
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


class TestMemoryAdapter:
    @pytest.mark.asyncio
    async def test_write_read(self):
        a = MemoryAdapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.read_chunk(ref) == enc

    @pytest.mark.asyncio
    async def test_delete(self):
        a = MemoryAdapter()
        ref = await a.write_chunk(_sample_encoded())
        assert await a.delete_chunk(ref)
        assert not await a.delete_chunk(ref)  # already gone

    @pytest.mark.asyncio
    async def test_list(self):
        a = MemoryAdapter()
        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m1", 0, 1))
        await a.write_chunk(_sample_encoded("m2", 0, 0))
        refs = await a.list_chunks("m1")
        assert len(refs) == 2

    @pytest.mark.asyncio
    async def test_read_missing_raises(self):
        a = MemoryAdapter()
        from picard.adapters.base import StoredChunkRef
        with pytest.raises(KeyError):
            await a.read_chunk(StoredChunkRef("memory", "mem://nope/0/0"))


class TestFilesystemAdapter:
    @pytest.mark.asyncio
    async def test_write_read(self):
        with tempfile.TemporaryDirectory() as td:
            a = FilesystemAdapter(td)
            enc = _sample_encoded()
            ref = await a.write_chunk(enc)
            assert await a.read_chunk(ref) == enc

    @pytest.mark.asyncio
    async def test_delete(self):
        with tempfile.TemporaryDirectory() as td:
            a = FilesystemAdapter(td)
            ref = await a.write_chunk(_sample_encoded())
            assert await a.delete_chunk(ref)
            assert not await a.delete_chunk(ref)

    @pytest.mark.asyncio
    async def test_list(self):
        with tempfile.TemporaryDirectory() as td:
            a = FilesystemAdapter(td)
            await a.write_chunk(_sample_encoded("m1", 0, 0))
            await a.write_chunk(_sample_encoded("m1", 0, 1))
            refs = await a.list_chunks("m1")
            assert len(refs) == 2

    @pytest.mark.asyncio
    async def test_health_check(self):
        with tempfile.TemporaryDirectory() as td:
            a = FilesystemAdapter(td)
            assert await a.health_check()
