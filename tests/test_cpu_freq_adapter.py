"""Unit tests for cpu_freq_adapter (fallback mode works cross-platform)."""
import asyncio
import pytest
from unittest.mock import patch
from pathlib import Path

# Force fallback mode by patching AVAIL_FREQS.exists to return False
with patch.object(Path, "exists", return_value=False):
    from picard.adapters.cpu_freq_adapter import CpuFreqAdapter

from picard.adapters.base import StoredChunkRef

CHUNK = "hello"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _adapter():
    with patch("pathlib.Path.exists", return_value=False):
        return CpuFreqAdapter()


def test_profile():
    a = _adapter()
    p = a.profile()
    assert p.name == "cpu-freq"
    assert a._mode == "fallback"


def test_health_check():
    a = _adapter()
    result = _run(a.health_check())
    assert result is True


def test_write_read_delete():
    a = _adapter()
    ref = _run(a.write_chunk(CHUNK))
    assert ref.adapter_name == "cpu-freq"

    data = _run(a.read_chunk(ref))
    assert data == CHUNK

    deleted = _run(a.delete_chunk(ref))
    assert deleted is True

    with pytest.raises(KeyError):
        _run(a.read_chunk(ref))


def test_list_chunks():
    a = _adapter()
    _run(a.write_chunk(CHUNK))
    refs = _run(a.list_chunks(""))
    assert len(refs) == 1
