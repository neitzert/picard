"""Tests for HpackTableAdapter."""
from __future__ import annotations

import pytest

from picard.adapters.hpack_table_adapter import HpackTableAdapter


def make_chunk(manifest_id: str = "test-manifest", idx: int = 0) -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(12)).decode()
    return f"PCHK:2:{manifest_id}:{idx}:{idx}:1:{payload}"


@pytest.fixture
def adapter():
    return HpackTableAdapter()


@pytest.mark.asyncio
async def test_health_check(adapter):
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_profile_fields(adapter):
    p = adapter.profile()
    assert p.name == "hpack-table"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_write_read_roundtrip(adapter):
    chunk = make_chunk(idx=0)
    ref = await adapter.write_chunk(chunk)
    assert ref.adapter_name == "hpack-table"
    assert "hpack://" in ref.location
    result = await adapter.read_chunk(ref)
    assert result == chunk


@pytest.mark.asyncio
async def test_delete_prevents_read(adapter):
    chunk = make_chunk(idx=1)
    ref = await adapter.write_chunk(chunk)
    deleted = await adapter.delete_chunk(ref)
    assert deleted is True
    with pytest.raises(KeyError):
        await adapter.read_chunk(ref)


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(adapter):
    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="hpack-table", location="hpack://deadbeef12345678")
    result = await adapter.delete_chunk(ref)
    assert result is False


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id(adapter):
    mid = "hpack-manifest"
    c1 = make_chunk(mid, 0)
    c2 = make_chunk(mid, 1)
    c3 = make_chunk("other", 0)
    ref1 = await adapter.write_chunk(c1)
    ref2 = await adapter.write_chunk(c2)
    await adapter.write_chunk(c3)
    listed = await adapter.list_chunks(mid)
    locs = {r.location for r in listed}
    assert ref1.location in locs
    assert ref2.location in locs


@pytest.mark.asyncio
async def test_works_without_hpack_library(monkeypatch):
    """Adapter should work even if hpack is not installed."""
    import sys
    # Patch out hpack import
    monkeypatch.setitem(sys.modules, "hpack", None)
    a = HpackTableAdapter()
    # Force re-init without hpack
    a._hpack_available = False
    a._encoder = None
    chunk = make_chunk(idx=0)
    ref = await a.write_chunk(chunk)
    result = await a.read_chunk(ref)
    assert result == chunk


@pytest.mark.asyncio
async def test_multiple_chunks_independent(adapter):
    c1 = make_chunk("m1", 0)
    c2 = make_chunk("m2", 0)
    ref1 = await adapter.write_chunk(c1)
    ref2 = await adapter.write_chunk(c2)
    assert await adapter.read_chunk(ref1) == c1
    assert await adapter.read_chunk(ref2) == c2
