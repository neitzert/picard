"""Tests for NftablesAdapter."""

import sys
import pytest

if sys.platform != "linux":
    pytest.skip("nftables adapter requires Linux", allow_module_level=True)

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from picard.adapters.nftables_adapter import NftablesAdapter


CHUNK = "PCHK:2:test-manifest:0:0:1:dGVzdHBheWxvYWQ="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestProfile(unittest.TestCase):
    def test_profile_fields(self):
        a = NftablesAdapter()
        p = a.profile()
        assert p.name == "nftables"
        assert p.max_chunk_bytes == 200
        assert p.persistent is False
        assert p.ordered is False


class TestHealthCheck(unittest.TestCase):
    def test_returns_true_when_nft_present(self):
        with patch("shutil.which", return_value="/usr/sbin/nft"):
            a = NftablesAdapter()
            assert run(a.health_check()) is True

    def test_returns_false_when_nft_absent(self):
        with patch("shutil.which", return_value=None):
            a = NftablesAdapter()
            assert run(a.health_check()) is False


class TestWriteRead(unittest.TestCase):
    def _make_adapter_with_mock_nft(self, rc=0):
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(rc, ""))
        return a

    def test_write_read_roundtrip(self):
        a = self._make_adapter_with_mock_nft()
        ref = run(a.write_chunk(CHUNK))
        assert ref.adapter_name == "nftables"
        assert run(a.read_chunk(ref)) == CHUNK

    def test_write_raises_on_oversized_chunk(self):
        a = NftablesAdapter(max_chunk_bytes=10)
        with pytest.raises(ValueError, match="too large"):
            run(a.write_chunk(CHUNK))

    def test_write_raises_on_nft_failure(self):
        a = self._make_adapter_with_mock_nft(rc=1)
        with pytest.raises(PermissionError):
            run(a.write_chunk(CHUNK))

    def test_read_raises_for_unknown_ref(self):
        from picard.adapters.base import StoredChunkRef
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        ref = StoredChunkRef(adapter_name="nftables", location="nftables://unknown")
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))


class TestDeleteChunk(unittest.TestCase):
    def test_delete_returns_true_when_exists(self):
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        ref = run(a.write_chunk(CHUNK))
        assert run(a.delete_chunk(ref)) is True

    def test_delete_prevents_read(self):
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        ref = run(a.write_chunk(CHUNK))
        run(a.delete_chunk(ref))
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))

    def test_delete_returns_false_when_absent(self):
        from picard.adapters.base import StoredChunkRef
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        ref = StoredChunkRef(adapter_name="nftables", location="nftables://ghost")
        assert run(a.delete_chunk(ref)) is False


class TestListChunks(unittest.TestCase):
    def test_list_chunks(self):
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("test-manifest"))
        assert len(listed) == 1

    def test_list_chunks_filters_by_manifest(self):
        a = NftablesAdapter()
        a._run_nft = AsyncMock(return_value=(0, ""))
        run(a.write_chunk(CHUNK))
        listed = run(a.list_chunks("other-manifest"))
        assert len(listed) == 0
