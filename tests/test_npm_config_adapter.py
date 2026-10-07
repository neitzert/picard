"""Tests for the NPM Config adapter -- uses temp files, no real .npmrc changes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.npm_config_adapter import NpmConfigAdapter
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


def _make_adapter(tmp_path: str | None = None, **kwargs) -> NpmConfigAdapter:
    if tmp_path is None:
        tmp_path = os.path.join(tempfile.mkdtemp(), ".npmrc")
    defaults = dict(npmrc_path=tmp_path, prefix="picard")
    defaults.update(kwargs)
    return NpmConfigAdapter(**defaults)


@pytest.mark.asyncio
class TestNpmConfigAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "npm-config"
        assert p.max_chunk_bytes == 8192
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "npm-config"
        assert ref.location.startswith("npmrc://")
        assert a._cache[ref.location] == enc
        # Verify file was written
        content = Path(a._npmrc_path).read_text(encoding="utf-8")
        assert enc in content

    async def test_write_multiple_chunks(self):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)

        ref1 = await a.write_chunk(enc1)
        ref2 = await a.write_chunk(enc2)

        assert ref1.location != ref2.location
        content = Path(a._npmrc_path).read_text(encoding="utf-8")
        assert enc1 in content
        assert enc2 in content

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_from_file(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        # Clear cache to force file read
        a._cache.clear()

        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="npm-config", location="npmrc:///fake/.npmrc/missing123")

        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.delete_chunk(ref)

        assert result is True
        assert ref.location not in a._cache
        # Verify line removed from file
        content = Path(a._npmrc_path).read_text(encoding="utf-8")
        assert enc not in content

    async def test_delete_chunk_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="npm-config", location="npmrc:///fake/.npmrc/nope")

        result = await a.delete_chunk(ref)
        assert result is False

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
        assert all(r.adapter_name == "npm-config" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        assert await a.health_check() is True

    async def test_health_check_with_existing_file(self):
        a = _make_adapter()
        # Write something so file exists
        await a.write_chunk(_sample_encoded())
        assert await a.health_check() is True

    async def test_capacity(self):
        a = _make_adapter()
        enc = _sample_encoded()
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None

    async def test_capacity_empty(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_write_chunk_bad_path(self):
        # Point to a path that can't be created
        a = _make_adapter(tmp_path="/dev/null/impossible/.npmrc")
        with pytest.raises(RuntimeError, match="failed to write"):
            await a.write_chunk(_sample_encoded())
