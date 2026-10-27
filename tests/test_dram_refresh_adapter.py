"""Unit tests for dram_refresh_adapter (Linux-only)."""
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("dram_refresh_adapter requires Linux", allow_module_level=True)

import asyncio
from unittest.mock import patch
from picard.adapters.dram_refresh_adapter import DramRefreshAdapter
from picard.adapters.base import StoredChunkRef

SHORT_CHUNK = "hello"  # 5 bytes, within 6-byte limit


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_profile():
    a = DramRefreshAdapter()
    p = a.profile()
    assert p.name == "dram-refresh"
    assert p.max_chunk_bytes == 6


def test_chunk_too_large():
    a = DramRefreshAdapter()
    big = "x" * 7

    with pytest.raises(ValueError):
        _run(a.write_chunk(big))


def test_write_read_delete():
    a = DramRefreshAdapter()
    orig_vals = [60, 20, 10, 100, 50, 0]
    read_call_count = [0]

    def _read_param(path):
        # On write, return orig values; on read, return encoded bytes
        if a._registry:
            # After write: return the encoded byte values
            encoded = SHORT_CHUNK.encode().ljust(6, b"\x00")
            return encoded[read_call_count[0] % 6]
        val = orig_vals[read_call_count[0] % 6]
        read_call_count[0] += 1
        return val

    with patch("picard.adapters.dram_refresh_adapter._read_param", side_effect=lambda p: orig_vals.pop(0)), \
         patch("picard.adapters.dram_refresh_adapter._write_param"):
        ref = _run(a.write_chunk(SHORT_CHUNK))

    assert ref.adapter_name == "dram-refresh"

    encoded_bytes = list(SHORT_CHUNK.encode().ljust(6, b"\x00"))
    with patch("picard.adapters.dram_refresh_adapter._read_param", side_effect=lambda p: encoded_bytes.pop(0)):
        data_back = _run(a.read_chunk(ref))
    assert data_back == SHORT_CHUNK

    with patch("picard.adapters.dram_refresh_adapter._write_param"):
        deleted = _run(a.delete_chunk(ref))
    assert deleted is True
    assert len(a._registry) == 0


def test_list_chunks():
    a = DramRefreshAdapter()
    orig_vals = [60, 20, 10, 100, 50, 0]
    with patch("picard.adapters.dram_refresh_adapter._read_param", side_effect=lambda p: orig_vals.pop(0)), \
         patch("picard.adapters.dram_refresh_adapter._write_param"):
        _run(a.write_chunk(SHORT_CHUNK))

    refs = _run(a.list_chunks(""))
    assert len(refs) == 1


def test_health_check():
    a = DramRefreshAdapter()
    with patch("pathlib.Path.exists", return_value=True):
        result = _run(a.health_check())
    assert result is True
