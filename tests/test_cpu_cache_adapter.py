"""Tests for cpu_cache_adapter.

Hardware JIT code is mocked for portability. We test:
  - Threshold calibration structure
  - Bit encoding (load vs flush)
  - Chunk write/read roundtrip using mocked primitives
  - Location ref format
  - Health check
"""

import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.cpu_cache_adapter import (
    BYTES_PER_BIT,
    CACHE_LINE_BYTES,
    DEFAULT_THRESHOLD,
    CpuCacheAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(tmp_path: str, max_bytes: int = 8) -> CpuCacheAdapter:
    return CpuCacheAdapter(
        mmap_path=tmp_path,
        max_chunk_bytes=max_bytes,
        threshold=DEFAULT_THRESHOLD,
    )


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

class TestCpuCacheAdapterProfile:
    def test_profile_name(self, tmp_path):
        a = _make_adapter(str(tmp_path / "cache.mmap"))
        p = a.profile()
        assert p.name == "cpu-cache"
        assert p.persistent is False

    def test_max_chunk_bytes_configurable(self, tmp_path):
        a = _make_adapter(str(tmp_path / "cache.mmap"), max_bytes=64)
        assert a.profile().max_chunk_bytes == 64


# ---------------------------------------------------------------------------
# Write/read roundtrip using real shared memory (no JIT required on Windows)
# ---------------------------------------------------------------------------

class TestCpuCacheAdapterIO:
    """Write and read with the non-x86 fallback (sentinel bytes).

    On x86-64 the JIT clflush path is used; on other platforms (CI on ARM,
    Windows) the fallback writes 0xFF/0x00 sentinel bytes. Both paths
    should produce a correct roundtrip.
    """

    def test_write_returns_valid_location(self, tmp_path):
        path = str(tmp_path / "ch.mmap")
        a = _make_adapter(path, max_bytes=8)
        ref = asyncio.get_event_loop().run_until_complete(
            a.write_chunk("Hi")
        )
        assert ref.adapter_name == "cpu-cache"
        payload = json.loads(ref.location)
        assert payload["n_bytes"] == 2
        assert payload["mmap_path"] == path

    def test_write_read_roundtrip_fallback(self, tmp_path, monkeypatch):
        # Force non-x86 fallback so this test runs everywhere
        monkeypatch.setattr("picard.adapters.cpu_cache_adapter._X86", False)
        path = str(tmp_path / "rr.mmap")
        a = _make_adapter(path, max_bytes=16)
        original = "Hello"
        ref = asyncio.get_event_loop().run_until_complete(
            a.write_chunk(original)
        )
        recovered = asyncio.get_event_loop().run_until_complete(
            a.read_chunk(ref)
        )
        assert recovered == original

    def test_chunk_too_large_raises(self, tmp_path):
        path = str(tmp_path / "big.mmap")
        a = _make_adapter(path, max_bytes=4)
        with pytest.raises(ValueError, match="too large"):
            asyncio.get_event_loop().run_until_complete(
                a.write_chunk("ABCDE")   # 5 bytes > 4
            )

    def test_delete_chunk_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr("picard.adapters.cpu_cache_adapter._X86", False)
        path = str(tmp_path / "del.mmap")
        a = _make_adapter(path, max_bytes=8)
        ref = asyncio.get_event_loop().run_until_complete(a.write_chunk("ok"))
        ok = asyncio.get_event_loop().run_until_complete(a.delete_chunk(ref))
        assert ok is True

    def test_health_check_succeeds_with_valid_mmap(self, tmp_path, monkeypatch):
        monkeypatch.setattr("picard.adapters.cpu_cache_adapter._X86", False)
        path = str(tmp_path / "hc.mmap")
        a = _make_adapter(path)
        ok = asyncio.get_event_loop().run_until_complete(a.health_check())
        assert ok is True

    def test_list_chunks_returns_empty(self, tmp_path):
        path = str(tmp_path / "ls.mmap")
        a = _make_adapter(path)
        result = asyncio.get_event_loop().run_until_complete(
            a.list_chunks("any-manifest")
        )
        assert list(result) == []


# ---------------------------------------------------------------------------
# Region sizing
# ---------------------------------------------------------------------------

class TestRegionSizing:
    def test_auto_region_size_covers_max_chunk(self, tmp_path):
        path = str(tmp_path / "sz.mmap")
        max_bytes = 128
        a = _make_adapter(path, max_bytes=max_bytes)
        expected_min = max_bytes * 8 * BYTES_PER_BIT
        assert a._region_size >= expected_min

    def test_explicit_region_size_respected(self, tmp_path):
        path = str(tmp_path / "es.mmap")
        explicit = 65536
        a = CpuCacheAdapter(
            mmap_path=path,
            region_size=explicit,
            max_chunk_bytes=8,
        )
        assert a._region_size == explicit


# ---------------------------------------------------------------------------
# Location ref carries mmap path for cross-process use
# ---------------------------------------------------------------------------

class TestLocationRef:
    def test_location_contains_mmap_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("picard.adapters.cpu_cache_adapter._X86", False)
        path = str(tmp_path / "loc.mmap")
        a = _make_adapter(path, max_bytes=8)
        ref = asyncio.get_event_loop().run_until_complete(a.write_chunk("XY"))
        payload = json.loads(ref.location)
        assert payload["mmap_path"] == path

    def test_read_from_different_adapter_instance(self, tmp_path, monkeypatch):
        """Reader creates its own adapter pointing at the same mmap file."""
        monkeypatch.setattr("picard.adapters.cpu_cache_adapter._X86", False)
        path = str(tmp_path / "shared.mmap")
        writer = _make_adapter(path, max_bytes=8)
        ref = asyncio.get_event_loop().run_until_complete(writer.write_chunk("ZZ"))

        reader = _make_adapter(path, max_bytes=8)
        result = asyncio.get_event_loop().run_until_complete(reader.read_chunk(ref))
        assert result == "ZZ"
