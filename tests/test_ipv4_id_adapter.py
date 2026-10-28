"""Tests for Ipv4IdAdapter."""
from __future__ import annotations

import sys
import pytest

if sys.platform != "linux":
    pytest.skip("ipv4_id_adapter requires Linux", allow_module_level=True)

from picard.adapters.ipv4_id_adapter import Ipv4IdAdapter


def make_chunk(manifest_id: str = "test-manifest", idx: int = 0) -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(12)).decode()
    return f"PCHK:2:{manifest_id}:{idx}:{idx}:1:{payload}"


@pytest.fixture
def adapter(tmp_path):
    sock = str(tmp_path / "ipv4id.sock")
    return Ipv4IdAdapter(companion_socket=sock, target_host="127.0.0.1", target_port=19877)


@pytest.mark.asyncio
async def test_health_check(adapter):
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_profile_fields(adapter):
    p = adapter.profile()
    assert p.name == "ipv4-id"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_write_read_roundtrip(adapter):
    chunk = make_chunk(idx=0)
    ref = await adapter.write_chunk(chunk)
    assert ref.adapter_name == "ipv4-id"
    assert "ipv4-id://" in ref.location
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
async def test_list_chunks_by_manifest_id(adapter):
    mid = "ipv4id-manifest"
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
async def test_delete_nonexistent_returns_false(adapter):
    from picard.adapters.base import StoredChunkRef
    ref = StoredChunkRef(adapter_name="ipv4-id", location="ipv4-id://deadbeef12345678")
    result = await adapter.delete_chunk(ref)
    assert result is False
