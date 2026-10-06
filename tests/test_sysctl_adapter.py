"""Tests for the sysctl adapter -- mocked, no real kernel access."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sysctl_adapter import SysctlAdapter
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


def _make_adapter(**kwargs) -> SysctlAdapter:
    defaults = dict(prefix="net.picard", max_chunk_bytes=1024)
    defaults.update(kwargs)
    return SysctlAdapter(**defaults)


@pytest.mark.asyncio
class TestSysctlAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "sysctl"
        assert p.max_chunk_bytes == 1024
        assert p.persistent is False
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "sysctl"
        assert ref.location.startswith("sysctl://net.picard.")
        assert enc in a._cache.values()

    async def test_write_chunk_sysctl_failure_still_caches(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(side_effect=RuntimeError("permission denied"))
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "sysctl"
        assert a._cache[ref.location] == enc

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_fallback_to_sysctl(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._run_sysctl = AsyncMock(return_value=enc)

        ref = StoredChunkRef(adapter_name="sysctl", location="sysctl://net.picard.abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        a._run_sysctl.assert_awaited_once_with("-n", "net.picard.abc123")

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(side_effect=RuntimeError("not found"))

        ref = StoredChunkRef(adapter_name="sysctl", location="sysctl://net.picard.missing")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        assert ref.location in a._cache

        result = await a.delete_chunk(ref)
        assert result is True
        assert ref.location not in a._cache

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")

        ref = StoredChunkRef(adapter_name="sysctl", location="sysctl://net.picard.missing")
        result = await a.delete_chunk(ref)
        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        await a.write_chunk(enc1)
        await a.write_chunk(enc2)
        await a.write_chunk(enc_other)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "sysctl" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="some.param = 1")
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(side_effect=RuntimeError("no access"))
        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        a._run_sysctl = AsyncMock(return_value="")
        enc = _sample_encoded()

        await a.write_chunk(enc)
        cap = await a.capacity()

        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
