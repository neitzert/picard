"""Tests for the NATS adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.nats_adapter import NatsAdapter


@pytest.fixture
def adapter():
    return NatsAdapter(servers="nats://localhost:4222", bucket="test-bucket")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "nats"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 5000


@pytest.fixture
def mock_nats(monkeypatch):
    """Set up mock nats module."""
    mock_kv = AsyncMock()
    mock_kv.put = AsyncMock()
    mock_kv.get = AsyncMock()
    mock_kv.delete = AsyncMock()
    mock_kv.keys = AsyncMock(return_value=[])
    mock_kv.status = AsyncMock(return_value=SimpleNamespace(bytes=1024, values=10))

    mock_js = AsyncMock()
    mock_js.key_value = AsyncMock(return_value=mock_kv)
    mock_js.create_key_value = AsyncMock(return_value=mock_kv)

    mock_nc = AsyncMock()
    mock_nc.jetstream = MagicMock(return_value=mock_js)
    mock_nc.close = AsyncMock()

    mock_connect = AsyncMock(return_value=mock_nc)

    import sys
    mock_mod = MagicMock()
    mock_mod.connect = mock_connect
    sys.modules["nats"] = mock_mod

    yield SimpleNamespace(nc=mock_nc, js=mock_js, kv=mock_kv, connect=mock_connect)

    del sys.modules["nats"]


@pytest.mark.asyncio
class TestWriteChunk:
    async def test_write_returns_ref(self, adapter, mock_nats):
        ref = await adapter.write_chunk("PCHK:1:abc123:0:0:1:dGVzdA==")
        assert ref.adapter_name == "nats"
        assert ref.location == "nats://test-bucket/abc123.0.0"
        mock_nats.kv.put.assert_called_once()

    async def test_write_opaque_data(self, adapter, mock_nats):
        ref = await adapter.write_chunk("deadbeef0102030405")
        assert ref.adapter_name == "nats"
        assert ref.location.startswith("nats://test-bucket/")


@pytest.mark.asyncio
class TestReadChunk:
    async def test_read_returns_data(self, adapter, mock_nats):
        entry = SimpleNamespace(value=b"PCHK:1:abc123:0:0:1:dGVzdA==")
        mock_nats.kv.get = AsyncMock(return_value=entry)

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="nats", location="nats://test-bucket/abc123.0.0")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc123:0:0:1:dGVzdA=="


@pytest.mark.asyncio
class TestDeleteChunk:
    async def test_delete_success(self, adapter, mock_nats):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="nats", location="nats://test-bucket/abc123.0.0")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter, mock_nats):
        mock_nats.kv.delete = AsyncMock(side_effect=Exception("not found"))
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="nats", location="nats://test-bucket/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_filters_by_manifest(self, adapter, mock_nats):
        mock_nats.kv.keys = AsyncMock(return_value=[
            "abc123.0.0", "abc123.0.1", "other.0.0",
        ])
        refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2
        assert all(r.adapter_name == "nats" for r in refs)

    async def test_list_empty(self, adapter, mock_nats):
        mock_nats.kv.keys = AsyncMock(return_value=[])
        refs = await adapter.list_chunks("abc123")
        assert refs == []


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter, mock_nats):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter, mock_nats):
        mock_nats.connect.side_effect = Exception("refused")
        assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_returns_stats(self, adapter, mock_nats):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 1024
        assert cap["chunk_count"] == 10
