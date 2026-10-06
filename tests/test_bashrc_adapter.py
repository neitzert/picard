"""Tests for the bashrc adapter -- uses tmp files, no real .bashrc needed."""

from __future__ import annotations

import os
import tempfile

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.bashrc_adapter import BashrcAdapter
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


def _make_adapter(rc_path: str | None = None, **kwargs) -> BashrcAdapter:
    if rc_path is None:
        # Create a temp file to act as the bashrc
        fd, rc_path = tempfile.mkstemp(prefix="bashrc_test_", suffix=".sh")
        os.close(fd)
    defaults = dict(rc_path=rc_path, max_chunk_bytes=4096)
    defaults.update(kwargs)
    return BashrcAdapter(**defaults)


@pytest.mark.asyncio
class TestBashrcAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "bashrc"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "bashrc"
        assert ref.location.startswith("bashrc://")
        assert a._cache[ref.location] == enc

        # Verify the file was written
        with open(a._rc_path, "r") as f:
            content = f.read()
        assert "# PICARD:" in content
        assert enc in content

        os.unlink(a._rc_path)

    async def test_write_multiple_chunks(self):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)

        ref1 = await a.write_chunk(enc1)
        ref2 = await a.write_chunk(enc2)

        assert ref1.location != ref2.location
        assert len(a._cache) == 2

        with open(a._rc_path, "r") as f:
            lines = [l for l in f.readlines() if l.startswith("# PICARD:")]
        assert len(lines) == 2

        os.unlink(a._rc_path)

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

        os.unlink(a._rc_path)

    async def test_read_chunk_from_file(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        # Clear the cache to force file read
        a._cache.clear()

        result = await a.read_chunk(ref)
        assert result == enc

        os.unlink(a._rc_path)

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="bashrc", location="bashrc:///tmp/fake/.bashrc/deadbeef")

        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

        os.unlink(a._rc_path)

    async def test_delete_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.delete_chunk(ref)
        assert result is True
        assert ref.location not in a._cache

        # Verify the line was removed from the file
        with open(a._rc_path, "r") as f:
            content = f.read()
        assert "PICARD:" not in content

        os.unlink(a._rc_path)

    async def test_delete_chunk_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="bashrc", location="bashrc:///fake/path/deadbeef")

        result = await a.delete_chunk(ref)
        assert result is False

        os.unlink(a._rc_path)

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
        assert all(r.adapter_name == "bashrc" for r in refs)

        os.unlink(a._rc_path)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

        os.unlink(a._rc_path)

    async def test_health_check(self):
        a = _make_adapter()
        assert await a.health_check() is True

        os.unlink(a._rc_path)

    async def test_health_check_bad_path(self):
        a = _make_adapter(rc_path="/nonexistent/deeply/nested/path/.bashrc")
        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        enc = _sample_encoded()
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == len(enc.encode())
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None

        os.unlink(a._rc_path)
