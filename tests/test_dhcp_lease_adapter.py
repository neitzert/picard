"""Tests for DhcpLeaseAdapter."""
from __future__ import annotations

import pytest

from picard.adapters.dhcp_lease_adapter import DhcpLeaseAdapter


def make_chunk(manifest_id: str = "test-manifest", idx: int = 0) -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(12)).decode()
    return f"PCHK:2:{manifest_id}:{idx}:{idx}:1:{payload}"


@pytest.fixture
def adapter(tmp_path):
    lease_file = str(tmp_path / "dnsmasq.leases")
    return DhcpLeaseAdapter(lease_file=lease_file)


@pytest.mark.asyncio
async def test_health_check(adapter):
    # lease file doesn't exist yet, dnsmasq likely not installed -- returns False but doesn't raise
    result = await adapter.health_check()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_health_check_with_existing_lease_file(adapter):
    # Create the lease file so health_check can find it
    import pathlib
    pathlib.Path(adapter._lease_file).touch()
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_profile_fields(adapter):
    p = adapter.profile()
    assert p.name == "dhcp-lease"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is True
    assert p.ordered is False


@pytest.mark.asyncio
async def test_write_read_roundtrip(adapter):
    chunk = make_chunk(idx=0)
    ref = await adapter.write_chunk(chunk)
    assert ref.adapter_name == "dhcp-lease"
    assert "dhcp-lease://" in ref.location
    result = await adapter.read_chunk(ref)
    assert result == chunk


@pytest.mark.asyncio
async def test_write_persists_to_file(adapter):
    import pathlib
    chunk = make_chunk(idx=0)
    await adapter.write_chunk(chunk)
    content = pathlib.Path(adapter._lease_file).read_text()
    assert "picard-" in content


@pytest.mark.asyncio
async def test_delete_prevents_read(adapter):
    chunk = make_chunk(idx=1)
    ref = await adapter.write_chunk(chunk)
    deleted = await adapter.delete_chunk(ref)
    assert deleted is True
    # After delete, registry is gone; read from file should fail too
    with pytest.raises(KeyError):
        await adapter.read_chunk(ref)


@pytest.mark.asyncio
async def test_delete_removes_from_file(adapter):
    import pathlib
    chunk = make_chunk(idx=0)
    ref = await adapter.write_chunk(chunk)
    token = ref.location.removeprefix("dhcp-lease://")
    await adapter.delete_chunk(ref)
    content = pathlib.Path(adapter._lease_file).read_text()
    assert f"picard-{token}-" not in content


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id(adapter):
    mid = "dhcp-manifest"
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
async def test_read_from_file_after_registry_clear(adapter):
    chunk = make_chunk(idx=0)
    ref = await adapter.write_chunk(chunk)
    # Simulate registry loss (e.g. new adapter instance)
    adapter._registry.clear()
    result = await adapter.read_chunk(ref)
    assert result == chunk


@pytest.mark.asyncio
async def test_large_chunk_splits_across_entries(adapter):
    import pathlib
    # Create a chunk that needs multiple lease entries (> 50 chars of base64)
    big_chunk = make_chunk(idx=0) + "X" * 200
    ref = await adapter.write_chunk(big_chunk)
    content = pathlib.Path(adapter._lease_file).read_text()
    token = ref.location.removeprefix("dhcp-lease://")
    # Should have multiple entries
    count = content.count(f"picard-{token}-")
    assert count > 1
    result = await adapter.read_chunk(ref)
    assert result == big_chunk
