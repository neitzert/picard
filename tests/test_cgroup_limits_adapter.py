"""Tests for CgroupLimitsAdapter."""

import sys
import pytest

if sys.platform != "linux":
    pytest.skip("cgroup-limits adapter requires Linux", allow_module_level=True)

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch, mock_open

from picard.adapters.cgroup_limits_adapter import CgroupLimitsAdapter


CHUNK = "PCHK:2:test-manifest:0:0:1:dGVzdHBheWxvYWQ="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestProfile(unittest.TestCase):
    def test_profile_fields(self):
        a = CgroupLimitsAdapter()
        p = a.profile()
        assert p.name == "cgroup-limits"
        assert p.persistent is False
        assert p.ordered is False


class TestHealthCheck(unittest.TestCase):
    def test_returns_true_when_cgroup_v2_present(self):
        with patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=True):
            a = CgroupLimitsAdapter()
            assert run(a.health_check()) is True

    def test_returns_false_when_cgroup_v2_absent(self):
        with patch("os.path.isfile", return_value=False):
            a = CgroupLimitsAdapter()
            assert run(a.health_check()) is False

    def test_returns_false_when_not_writable(self):
        with patch("os.path.isfile", return_value=True), \
             patch("os.access", return_value=False):
            a = CgroupLimitsAdapter()
            assert run(a.health_check()) is False


class TestWriteRead(unittest.TestCase):
    def test_write_read_roundtrip(self):
        a = CgroupLimitsAdapter(cgroup_root="/tmp/picard-test-cg")
        written_files = {}

        def fake_makedirs(path, exist_ok=False):
            pass

        def fake_open(path, mode="r"):
            if "w" in mode:
                m = MagicMock()
                m.__enter__ = lambda s: s
                m.__exit__ = MagicMock(return_value=False)
                m.write = lambda v: written_files.__setitem__(path, str(v))
                return m
            # reading
            val = written_files.get(path, "0")
            import io
            return io.StringIO(val)

        with patch("os.makedirs", side_effect=fake_makedirs), \
             patch("builtins.open", side_effect=fake_open):
            ref = run(a.write_chunk(CHUNK))
            assert ref.adapter_name == "cgroup-limits"
            result = run(a.read_chunk(ref))
            assert result == CHUNK

    def test_write_raises_on_oversized_chunk(self):
        a = CgroupLimitsAdapter(max_chunk_bytes=5)
        with pytest.raises(ValueError, match="too large"):
            run(a.write_chunk(CHUNK))

    def test_read_raises_for_unknown_ref(self):
        from picard.adapters.base import StoredChunkRef
        a = CgroupLimitsAdapter()
        ref = StoredChunkRef(adapter_name="cgroup-limits", location="cgroup-limits://ghost")
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))


class TestDeleteChunk(unittest.TestCase):
    def test_delete_returns_true_when_exists(self):
        a = CgroupLimitsAdapter(cgroup_root="/tmp/picard-test-cg")
        with patch("os.makedirs"), patch("builtins.open", mock_open()):
            ref = run(a.write_chunk(CHUNK))
        with patch("os.path.isdir", return_value=True), \
             patch("os.listdir", return_value=[]), \
             patch("os.rmdir"):
            assert run(a.delete_chunk(ref)) is True

    def test_delete_prevents_read(self):
        a = CgroupLimitsAdapter(cgroup_root="/tmp/picard-test-cg")
        with patch("os.makedirs"), patch("builtins.open", mock_open()):
            ref = run(a.write_chunk(CHUNK))
        with patch("os.path.isdir", return_value=False), patch("os.rmdir"):
            run(a.delete_chunk(ref))
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))

    def test_delete_returns_false_when_absent(self):
        from picard.adapters.base import StoredChunkRef
        a = CgroupLimitsAdapter()
        ref = StoredChunkRef(adapter_name="cgroup-limits", location="cgroup-limits://ghost")
        assert run(a.delete_chunk(ref)) is False


class TestListChunks(unittest.TestCase):
    def test_list_chunks(self):
        a = CgroupLimitsAdapter(cgroup_root="/tmp/picard-test-cg")
        with patch("os.makedirs"), patch("builtins.open", mock_open()):
            run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("test-manifest"))
        assert len(listed) == 1

    def test_list_chunks_filters_by_manifest(self):
        a = CgroupLimitsAdapter(cgroup_root="/tmp/picard-test-cg")
        with patch("os.makedirs"), patch("builtins.open", mock_open()):
            run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("other-manifest"))
        assert len(listed) == 0
