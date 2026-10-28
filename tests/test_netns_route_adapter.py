"""Tests for NetnsRouteAdapter."""

import sys
import pytest

if sys.platform != "linux":
    pytest.skip("netns-route adapter requires Linux", allow_module_level=True)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from picard.adapters.netns_route_adapter import NetnsRouteAdapter


CHUNK = "PCHK:2:test-manifest:0:0:1:dGVzdHBheWxvYWQ="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestProfile(unittest.TestCase):
    def test_profile_fields(self):
        a = NetnsRouteAdapter()
        p = a.profile()
        assert p.name == "netns-route"
        assert p.max_chunk_bytes == 200
        assert p.persistent is False
        assert p.ordered is False


class TestHealthCheck(unittest.TestCase):
    def test_returns_true_when_ip_present(self):
        with patch("shutil.which", return_value="/sbin/ip"):
            a = NetnsRouteAdapter()
            a._run_ip = AsyncMock(return_value=(0, ""))
            assert run(a.health_check()) is True

    def test_returns_false_when_ip_absent(self):
        with patch("shutil.which", return_value=None):
            a = NetnsRouteAdapter()
            assert run(a.health_check()) is False

    def test_returns_false_when_netns_list_fails(self):
        with patch("shutil.which", return_value="/sbin/ip"):
            a = NetnsRouteAdapter()
            a._run_ip = AsyncMock(return_value=(1, ""))
            assert run(a.health_check()) is False


class TestWriteRead(unittest.TestCase):
    def _make_adapter(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(0, "", ""))
        return a

    def test_write_read_roundtrip(self):
        a = self._make_adapter()
        ref = run(a.write_chunk(CHUNK))
        assert ref.adapter_name == "netns-route"
        assert run(a.read_chunk(ref)) == CHUNK

    def test_write_raises_on_oversized_chunk(self):
        a = NetnsRouteAdapter(max_chunk_bytes=5)
        with pytest.raises(ValueError, match="too large"):
            run(a.write_chunk(CHUNK))

    def test_write_raises_on_route_failure(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(1, "", "permission denied"))
        with pytest.raises(PermissionError):
            run(a.write_chunk(CHUNK))

    def test_read_raises_for_unknown_ref(self):
        from picard.adapters.base import StoredChunkRef
        a = NetnsRouteAdapter()
        ref = StoredChunkRef(adapter_name="netns-route", location="netns-route://ghost")
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))


class TestDeleteChunk(unittest.TestCase):
    def test_delete_returns_true_when_exists(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(0, "", ""))
        ref = run(a.write_chunk(CHUNK))
        assert run(a.delete_chunk(ref)) is True

    def test_delete_prevents_read(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(0, "", ""))
        ref = run(a.write_chunk(CHUNK))
        run(a.delete_chunk(ref))
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))

    def test_delete_returns_false_when_absent(self):
        from picard.adapters.base import StoredChunkRef
        a = NetnsRouteAdapter()
        ref = StoredChunkRef(adapter_name="netns-route", location="netns-route://ghost")
        assert run(a.delete_chunk(ref)) is False


class TestListChunks(unittest.TestCase):
    def test_list_chunks(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(0, "", ""))
        run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("test-manifest"))
        assert len(listed) == 1

    def test_list_chunks_filters_by_manifest(self):
        a = NetnsRouteAdapter()
        a._run_ip = AsyncMock(return_value=(0, ""))
        a._netns_cmd = AsyncMock(return_value=(0, "", ""))
        run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("other-manifest"))
        assert len(listed) == 0
