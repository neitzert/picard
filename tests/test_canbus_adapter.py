"""Tests for CAN bus adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.canbus_adapter import CanBusAdapter


@pytest.fixture
def adapter():
    return CanBusAdapter(interface="vcan0", arbitration_id=0x7FF)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "canbus"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 6000

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_write_returns_ref(self, mock_thread, adapter):
        mock_thread.return_value = 2
        ref = await adapter.write_chunk("PCHK:test:0:0:data")
        assert ref.adapter_name == "canbus"
        assert "can://" in ref.location
        assert "0x7ff" in ref.location

    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_write_caches_data(self, mock_thread, adapter):
        mock_thread.return_value = 1
        ref = await adapter.write_chunk("PCHK:test:0:0:data")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:data"

    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_write_opaque_data(self, mock_thread, adapter):
        mock_thread.return_value = 1
        ref = await adapter.write_chunk("deadbeef0123456789")
        assert ref.adapter_name == "canbus"


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_delete_existing(self, mock_thread, adapter):
        mock_thread.return_value = 1
        ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_delete_nonexistent(self, mock_thread, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="canbus", location="can://vcan0/0x7ff/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_list_by_manifest(self, mock_thread, adapter):
        mock_thread.return_value = 1
        await adapter.write_chunk("PCHK:1:mfst1:0:0:1:ZGF0YQ==")
        refs = await adapter.list_chunks("mfst1")
        assert len(refs) == 1

    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_list_empty(self, mock_thread, adapter):
        refs = await adapter.list_chunks("nope")
        assert refs == []


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        with patch("picard.adapters.canbus_adapter.asyncio.to_thread", return_value=True):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("picard.adapters.canbus_adapter.asyncio.to_thread", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    @patch("picard.adapters.canbus_adapter.asyncio.to_thread")
    async def test_capacity_with_data(self, mock_thread, adapter):
        mock_thread.return_value = 1
        await adapter.write_chunk("PCHK:cap:0:0:somedata")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
