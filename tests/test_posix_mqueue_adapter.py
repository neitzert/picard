"""Tests for PosixMqueueAdapter."""
from __future__ import annotations

import sys
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux only")


def _make_chunk(n: int = 8, idx: int = 0, mid: str = "test-manifest") -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(n)).decode()
    return f"PCHK:2:{mid}:{idx}:{idx}:1:{payload}"


def test_profile_fields():
    from picard.adapters.posix_mqueue_adapter import PosixMqueueAdapter
    p = PosixMqueueAdapter().profile()
    assert p.name == "posix-mqueue"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_health_check_mqueue_present():
    import os
    from picard.adapters.posix_mqueue_adapter import PosixMqueueAdapter
    a = PosixMqueueAdapter()
    # /dev/mqueue is standard on Linux
    result = await a.health_check()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_write_read_roundtrip():
    """Live test -- only runs if /dev/mqueue is accessible."""
    import os
    if not os.path.isdir("/dev/mqueue"):
        pytest.skip("/dev/mqueue not available")
    from picard.adapters.posix_mqueue_adapter import PosixMqueueAdapter
    a = PosixMqueueAdapter()
    chunk = _make_chunk(12)
    ref = await a.write_chunk(chunk)
    assert ref.adapter_name == "posix-mqueue"
    assert ref.location.startswith("mqueue:///picard.")
    got = await a.read_chunk(ref)
    assert got == chunk
    await a.delete_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_delete_prevents_read():
    import os
    if not os.path.isdir("/dev/mqueue"):
        pytest.skip("/dev/mqueue not available")
    from picard.adapters.posix_mqueue_adapter import PosixMqueueAdapter
    a = PosixMqueueAdapter()
    chunk = _make_chunk(8)
    ref = await a.write_chunk(chunk)
    assert await a.delete_chunk(ref) is True
    with pytest.raises(KeyError):
        await a.read_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id():
    import os
    if not os.path.isdir("/dev/mqueue"):
        pytest.skip("/dev/mqueue not available")
    from picard.adapters.posix_mqueue_adapter import PosixMqueueAdapter
    a = PosixMqueueAdapter()
    mid = "manifest-xyz"
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
    await a.close()


def test_location_format_mock():
    """Verify location format via mocked ctypes without needing /dev/mqueue."""
    import os
    from unittest.mock import patch, MagicMock
    from picard.adapters import posix_mqueue_adapter as mod

    mock_mqd = 5
    chunk = _make_chunk(8)
    chunk_bytes = chunk.encode()

    with patch.object(mod, "_mq_open_create", return_value=mock_mqd), \
         patch.object(mod._librt, "mq_send", return_value=0), \
         patch.object(mod._librt, "mq_close", return_value=0):
        import asyncio
        a = mod.PosixMqueueAdapter()
        # Manually inject registry state to test location format
        a._registry["/picard.testtoken12345"] = (chunk, "test-manifest")
        ref = asyncio.get_event_loop().run_until_complete(a.write_chunk(chunk))
        assert ref.location.startswith("mqueue:///picard.")
        assert ref.adapter_name == "posix-mqueue"
