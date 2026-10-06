"""Tests for the pip.conf adapter -- mocked, no real pip.conf access."""

from __future__ import annotations

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.pip_conf_adapter import PipConfAdapter
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


def _make_adapter(**kwargs) -> PipConfAdapter:
    defaults = dict(conf_path="/tmp/fake-pip/pip.conf")
    defaults.update(kwargs)
    return PipConfAdapter(**defaults)


@pytest.mark.asyncio
class TestPipConfAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "pip-conf"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "pip-conf"
        assert ref.location.startswith("pip-conf://")
        assert len(ref.location.split("://")[1]) == 12

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="pip-conf", location="pip-conf://nonexistent")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert await a.delete_chunk(ref) is True
        # Second delete returns False
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_nonexistent(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="pip-conf", location="pip-conf://nope")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        await a.write_chunk(enc1)
        await a.write_chunk(enc2)
        await a.write_chunk(enc_other)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "pip-conf" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check(self):
        a = _make_adapter()
        result = await a.health_check()
        assert result is True

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
        assert cap["used_bytes"] > 0
        assert cap["chunk_count"] == 1
