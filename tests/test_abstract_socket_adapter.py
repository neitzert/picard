"""Tests for AbstractSocketAdapter."""
from __future__ import annotations

import sys
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux only")

pytest_plugins = ("anyio",)


def _make_chunk(n: int = 8, idx: int = 0, mid: str = "test-manifest") -> str:
    import base64, os
    payload = base64.urlsafe_b64encode(os.urandom(n)).decode()
    return f"PCHK:2:{mid}:{idx}:{idx}:1:{payload}"


@pytest.fixture
def adapter():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    a = AbstractSocketAdapter()
    yield a
    import asyncio
    asyncio.get_event_loop().run_until_complete(a.close())


def test_profile_fields():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    p = AbstractSocketAdapter().profile()
    assert p.name == "abstract-socket"
    assert p.max_chunk_bytes == 65536
    assert p.persistent is False
    assert p.ordered is False


@pytest.mark.asyncio
async def test_health_check():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    a = AbstractSocketAdapter()
    assert await a.health_check() is True
    await a.close()


@pytest.mark.asyncio
async def test_write_read_roundtrip():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    a = AbstractSocketAdapter()
    chunk = _make_chunk(12)
    ref = await a.write_chunk(chunk)
    assert ref.adapter_name == "abstract-socket"
    assert ref.location.startswith("abstract-socket://")
    got = await a.read_chunk(ref)
    assert got == chunk
    await a.close()


@pytest.mark.asyncio
async def test_delete_prevents_read():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    a = AbstractSocketAdapter()
    chunk = _make_chunk(8)
    ref = await a.write_chunk(chunk)
    assert await a.delete_chunk(ref) is True
    with pytest.raises(KeyError):
        await a.read_chunk(ref)
    await a.close()


@pytest.mark.asyncio
async def test_list_chunks_by_manifest_id():
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    a = AbstractSocketAdapter()
    mid = "manifest-abc"
    c1 = _make_chunk(8, 0, mid)
    c2 = _make_chunk(8, 1, mid)
    c3 = _make_chunk(8, 0, "other-manifest")
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
    from picard.adapters.abstract_socket_adapter import AbstractSocketAdapter
    from picard.adapters.base import StoredChunkRef
    a = AbstractSocketAdapter()
    fake = StoredChunkRef(adapter_name="abstract-socket", location="abstract-socket://deadbeef")
    assert await a.delete_chunk(fake) is False
    await a.close()
