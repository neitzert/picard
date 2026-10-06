"""Tests for the NFS adapter -- uses a real temp dir (NFS mount simulated)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.nfs_adapter import NfsAdapter
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


@pytest.fixture
def nfs_dir(tmp_path):
    return tmp_path / "nfs_mount"


@pytest.fixture
def adapter(nfs_dir):
    nfs_dir.mkdir()
    return NfsAdapter(nfs_dir)


@pytest.mark.asyncio
class TestNfsAdapter:
    async def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "nfs"
        assert p.max_chunk_bytes == 1_048_576
        assert p.persistent is True
        assert p.rate_limit_per_min == 5000

    async def test_profile_custom_max(self, nfs_dir):
        nfs_dir.mkdir(exist_ok=True)
        a = NfsAdapter(nfs_dir, max_chunk_bytes=4096)
        assert a.profile().max_chunk_bytes == 4096

    async def test_write_read_round_trip(self, adapter):
        enc = _sample_encoded()
        ref = await adapter.write_chunk(enc)
        assert ref.adapter_name == "nfs"
        assert "m1" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == enc

    async def test_write_opaque_chunk(self, adapter):
        ref = await adapter.write_chunk("not-a-valid-chunk")
        assert ref.adapter_name == "nfs"
        result = await adapter.read_chunk(ref)
        assert result == "not-a-valid-chunk"

    async def test_multi_chunk_round_trip(self, adapter):
        enc0 = _sample_encoded(block=0, idx=0)
        enc1 = _sample_encoded(block=0, idx=1)
        ref0 = await adapter.write_chunk(enc0)
        ref1 = await adapter.write_chunk(enc1)

        assert await adapter.read_chunk(ref0) == enc0
        assert await adapter.read_chunk(ref1) == enc1

    async def test_delete_chunk(self, adapter):
        enc = _sample_encoded()
        ref = await adapter.write_chunk(enc)
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks(self, adapter):
        await adapter.write_chunk(_sample_encoded(block=0, idx=0))
        await adapter.write_chunk(_sample_encoded(block=0, idx=1))

        refs = await adapter.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "nfs" for r in refs)

    async def test_list_chunks_empty(self, adapter):
        refs = await adapter.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True

    async def test_health_check_bad_path(self):
        a = NfsAdapter("/nonexistent/path/for/nfs")
        assert await a.health_check() is False

    async def test_capacity(self, adapter):
        await adapter.write_chunk(_sample_encoded())
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["total_bytes"] is not None
        assert cap["free_bytes"] is not None
        assert cap["used_bytes"] is not None
        assert cap["used_bytes"] > 0

    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
