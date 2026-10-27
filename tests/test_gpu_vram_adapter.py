"""Unit tests for gpu_vram_adapter (in-process fallback, companion mocked)."""
import asyncio
import pytest
from unittest.mock import patch, AsyncMock
from picard.adapters.gpu_vram_adapter import GpuVramAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "hello"
PCHK_CHUNK = "PCHK:1:gpumid:0:0:1:YQ=="


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _no_companion(a):
    """Context manager that makes all companion connections fail with ConnectionError."""
    async def _fail():
        raise ConnectionError("no companion")
    return patch.object(a, "_connect", side_effect=_fail)


def _adapter_with_chunk(chunk=CHUNK):
    a = GpuVramAdapter(companion_socket="/tmp/picard_gpu_nonexistent.sock")
    token = "testtoken123"
    a._fallback[token] = chunk
    ref = StoredChunkRef(adapter_name="gpu_vram", location=token)
    return a, ref


def test_profile():
    a = GpuVramAdapter()
    p = a.profile()
    assert p.name == "gpu_vram"


def test_health_check_fallback():
    a = GpuVramAdapter()
    with _no_companion(a):
        result = _run(a.health_check())
    assert result is True


def test_read_from_fallback():
    a, ref = _adapter_with_chunk()
    data = _run(a.read_chunk(ref))
    assert data == CHUNK


def test_delete_from_fallback():
    a, ref = _adapter_with_chunk()
    with _no_companion(a):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True
    assert ref.location not in a._fallback


def test_read_missing_raises():
    a = GpuVramAdapter()
    ref = StoredChunkRef(adapter_name="gpu_vram", location="no_such_token")
    with _no_companion(a), pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks_fallback():
    a, _ = _adapter_with_chunk(PCHK_CHUNK)
    with _no_companion(a):
        refs = _run(a.list_chunks("gpumid"))
    assert len(refs) == 1
