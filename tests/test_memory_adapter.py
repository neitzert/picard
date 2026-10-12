"""Tests for the Memory adapter -- no mocking needed."""

from __future__ import annotations

import pytest

from picard.adapters.base import StoredChunkRef
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


def _make_adapter(**kwargs) -> MemoryAdapter:
    defaults: dict = {}
    defaults.update(kwargs)
    return MemoryAdapter(**defaults)


@pytest.mark.asyncio
class TestMemoryAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "memory"
        assert p.max_chunk_bytes == 1024
        assert p.persistent is False
        assert p.ordered is True

    async def test_profile_custom_name(self):
        a = _make_adapter(name="custom-mem", max_chunk_bytes=4096)
        p = a.profile()
        assert p.name == "custom-mem"
        assert p.max_chunk_bytes == 4096

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "memory"
        assert ref.location.startswith("mem://memory/m1/")
        assert a.store[ref.location] == enc

    async def test_write_chunk_fallback_uuid(self):
        a = _make_adapter()

        ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "memory"
        assert ref.location.startswith("mem://memory/")
        assert a.store[ref.location] == "not-a-valid-chunk"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="memory", location="mem://memory/missing")

        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        assert ref.location not in a.store

    async def test_delete_chunk_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="memory", location="mem://memory/missing")

        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc3 = _sample_encoded("m2", 0, 0)

        await a.write_chunk(enc1)
        await a.write_chunk(enc2)
        await a.write_chunk(enc3)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "memory" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check(self):
        a = _make_adapter()
        assert await a.health_check() is True

    async def test_capacity_empty(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None

    async def test_capacity_with_data(self):
        a = _make_adapter()
        enc = _sample_encoded()
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["used_bytes"] == len(enc.encode())
        assert cap["chunk_count"] == 1
