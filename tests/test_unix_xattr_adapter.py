"""Tests for the Unix xattr adapter."""

import os
from unittest.mock import patch, MagicMock

import pytest

from picard.adapters.unix_xattr import UnixXattrAdapter
from picard.adapters.base import StoredChunkRef

_HAS_XATTR = hasattr(os, "setxattr")


@pytest.fixture
def adapter(tmp_path):
    return UnixXattrAdapter(target_dir=tmp_path / "xattr_store", max_chunk_bytes=4096)


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "unix-xattr"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        with patch.object(os, "setxattr", create=True):
            ref = await adapter.write_chunk("test-chunk-data")
            assert ref.adapter_name == "unix-xattr"
            assert "xattr://" in ref.location

    async def test_read_returns_data(self, adapter):
        with patch.object(os, "setxattr", create=True):
            ref = await adapter.write_chunk("hello-xattr")
        with patch.object(os, "getxattr", return_value=b"hello-xattr", create=True):
            result = await adapter.read_chunk(ref)
            assert result == "hello-xattr"

    async def test_round_trip(self, adapter):
        data = "round-trip-xattr-data"
        with patch.object(os, "setxattr", create=True):
            ref = await adapter.write_chunk(data)
        with patch.object(os, "getxattr", return_value=data.encode(), create=True):
            result = await adapter.read_chunk(ref)
            assert result == data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter, tmp_path):
        store_dir = tmp_path / "xattr_store"
        store_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(os, "setxattr", create=True):
            ref = await adapter.write_chunk("delete-me")
        key = ref.location.rsplit("/", 1)[-1]
        (store_dir / key).touch()
        result = await adapter.delete_chunk(ref)
        assert result is True

    async def test_delete_missing(self, adapter):
        ref = StoredChunkRef(adapter_name="unix-xattr", location="xattr:///tmp/nonexistent/key")
        result = await adapter.delete_chunk(ref)
        assert result is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        refs = await adapter.list_chunks("nonexistent")
        assert refs == []

    async def test_list_matching(self, adapter, tmp_path):
        store_dir = tmp_path / "xattr_store"
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "manifest1_0_0").touch()
        (store_dir / "manifest1_0_1").touch()
        (store_dir / "other_0_0").touch()
        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        with patch.object(os, "setxattr", create=True), patch.object(os, "removexattr", create=True):
            result = await adapter.health_check()
            assert result is True

    async def test_health_fail(self, adapter):
        with patch.object(os, "setxattr", side_effect=OSError("no xattr"), create=True):
            result = await adapter.health_check()
            assert result is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] is None or cap["chunk_count"] == 0
