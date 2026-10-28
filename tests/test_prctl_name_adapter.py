"""Tests for PrctlNameAdapter."""
from __future__ import annotations

import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux only")


def _make_chunk(n: int = 8, idx: int = 0, mid: str = "test-manifest") -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(n)).decode()
    return f"PCHK:2:{mid}:{idx}:{idx}:1:{payload}"


def test_profile_fields():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    p = PrctlNameAdapter().profile()
    assert p.name == "prctl-name"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_health_check():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    a = PrctlNameAdapter()
    result = await a.health_check()
    assert result is True
    await a.close()


@pytest.mark.asyncio
async def test_write_read_roundtrip():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    a = PrctlNameAdapter()
    chunk = _make_chunk(12)
    ref = await a.write_chunk(chunk)
    assert ref.adapter_name == "prctl-name"
    assert ref.location.startswith("prctl-name://")
    got = await a.read_chunk(ref)
    assert got == chunk
    await a.delete_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_thread_name_set():
    """Verify that the daemon thread has the expected prctl name."""
    import threading
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    a = PrctlNameAdapter()
    chunk = _make_chunk(8)
    ref = await a.write_chunk(chunk)
    token = ref.location.removeprefix("prctl-name://")
    entry = a._threads[token]
    thread = entry[0]
    # The thread should be alive and named picard.<token8>
    assert thread.is_alive()
    # Read the thread name from /proc
    tid = thread.ident
    try:
        with open(f"/proc/self/task/{tid}/comm") as f:
            name = f.read().strip()
        assert name.startswith("picard.")
    except (FileNotFoundError, TypeError):
        pass  # thread may have already exited in tight timing
    await a.delete_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_delete_prevents_read():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    a = PrctlNameAdapter()
    chunk = _make_chunk(8)
    ref = await a.write_chunk(chunk)
    assert await a.delete_chunk(ref) is True
    with pytest.raises(KeyError):
        await a.read_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    a = PrctlNameAdapter()
    mid = "manifest-prctl"
    c1 = _make_chunk(8, 0, mid)
    c2 = _make_chunk(8, 1, mid)
    c3 = _make_chunk(8, 0, "other")
    r1 = await a.write_chunk(c1)
    r2 = await a.write_chunk(c2)
    await a.write_chunk(c3)
    listed = await a.list_chunks(mid)
    locs = {r.location for r in listed}
    assert r1.location in locs
    assert r2.location in locs
    assert len(listed) == 2
    await a.close()


@pytest.mark.asyncio
async def test_delete_returns_false_on_missing():
    from picard.adapters.prctl_name_adapter import PrctlNameAdapter
    from picard.adapters.base import StoredChunkRef
    a = PrctlNameAdapter()
    fake = StoredChunkRef(adapter_name="prctl-name", location="prctl-name://deadbeef1234")
    assert await a.delete_chunk(fake) is False
    await a.close()
