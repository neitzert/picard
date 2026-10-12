"""Tests for the STOMP adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.stomp_adapter import StompAdapter


@pytest.fixture
def adapter():
    return StompAdapter(host="localhost", port=61613, destination="/queue/test")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "stomp"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteChunk:
    async def test_write_returns_ref(self, adapter):
        mock_conn = MagicMock()
        mock_conn.connect = MagicMock()
        mock_conn.send = MagicMock()
        mock_conn.disconnect = MagicMock()

        import sys
        mock_stomp = MagicMock()
        mock_stomp.Connection = MagicMock(return_value=mock_conn)
        sys.modules["stomp"] = mock_stomp
        try:
            ref = await adapter.write_chunk("PCHK:1:abc123:0:0:1:dGVzdA==")
            assert ref.adapter_name == "stomp"
            assert "stomp://localhost/" in ref.location
            assert "abc123/0/0" in ref.location
        finally:
            del sys.modules["stomp"]

    async def test_write_opaque_data(self, adapter):
        mock_conn = MagicMock()
        mock_conn.connect = MagicMock()
        mock_conn.send = MagicMock()
        mock_conn.disconnect = MagicMock()

        import sys
        mock_stomp = MagicMock()
        mock_stomp.Connection = MagicMock(return_value=mock_conn)
        sys.modules["stomp"] = mock_stomp
        try:
            ref = await adapter.write_chunk("deadbeef0102030405")
            assert ref.adapter_name == "stomp"
            assert ref.location.startswith("stomp://localhost/")
        finally:
            del sys.modules["stomp"]

    async def test_write_caches_data(self, adapter):
        mock_conn = MagicMock()
        mock_conn.connect = MagicMock()
        mock_conn.send = MagicMock()
        mock_conn.disconnect = MagicMock()

        import sys
        mock_stomp = MagicMock()
        mock_stomp.Connection = MagicMock(return_value=mock_conn)
        sys.modules["stomp"] = mock_stomp
        try:
            await adapter.write_chunk("PCHK:1:abc123:0:0:1:dGVzdA==")
            assert len(adapter._cache) == 1
        finally:
            del sys.modules["stomp"]


@pytest.mark.asyncio
class TestReadChunk:
    async def test_read_from_cache(self, adapter):
        adapter._cache["abc123/0/0"] = "PCHK:1:abc123:0:0:1:dGVzdA=="
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="stomp",
            location="stomp://localhost//queue/test/abc123/0/0",
        )
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc123:0:0:1:dGVzdA=="


@pytest.mark.asyncio
class TestDeleteChunk:
    async def test_delete_cached(self, adapter):
        adapter._cache["abc123/0/0"] = "data"
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="stomp",
            location="stomp://localhost//queue/test/abc123/0/0",
        )
        assert await adapter.delete_chunk(ref) is True
        assert "abc123/0/0" not in adapter._cache

    async def test_delete_not_found(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="stomp",
            location="stomp://localhost//queue/test/nope",
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_filters_by_manifest(self, adapter):
        adapter._cache["abc123/0/0"] = "d1"
        adapter._cache["abc123/0/1"] = "d2"
        adapter._cache["other/0/0"] = "d3"
        refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2

    async def test_list_empty(self, adapter):
        refs = await adapter.list_chunks("nope")
        assert refs == []


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
        mock_conn = MagicMock()
        mock_conn.connect = MagicMock()
        mock_conn.disconnect = MagicMock()

        import sys
        mock_stomp = MagicMock()
        mock_stomp.Connection = MagicMock(return_value=mock_conn)
        sys.modules["stomp"] = mock_stomp
        try:
            assert await adapter.health_check() is True
        finally:
            del sys.modules["stomp"]

    async def test_unhealthy(self, adapter):
        import sys
        mock_stomp = MagicMock()
        mock_stomp.Connection = MagicMock(side_effect=Exception("refused"))
        sys.modules["stomp"] = mock_stomp
        try:
            assert await adapter.health_check() is False
        finally:
            del sys.modules["stomp"]


@pytest.mark.asyncio
class TestCapacity:
    async def test_returns_cached_stats(self, adapter):
        adapter._cache["k1"] = "data1"
        adapter._cache["k2"] = "data2"
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 2
        assert cap["used_bytes"] > 0
