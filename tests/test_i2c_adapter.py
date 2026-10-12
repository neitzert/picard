"""Tests for I2C EEPROM adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.i2c_adapter import I2cAdapter


@pytest.fixture
def adapter():
    return I2cAdapter(bus_number=1, device_address=0x50, page_size=32)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "i2c"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 256


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_write_returns_ref(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test123:0:0:hello")
        assert ref.adapter_name == "i2c"
        assert ref.location.startswith("i2c://")

    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_write_tracks_offset(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:abc:0:0:testdata")
        assert ref.location in adapter._offset_map

    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_write_opaque_data_uuid_fallback(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("deadbeef0123456789")
        assert ref.adapter_name == "i2c"
        assert "i2c://" in ref.location


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_delete_existing(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_delete_nonexistent(self, mock_thread, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="i2c", location="i2c://1/0x50/99999")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_list_empty(self, mock_thread, adapter):
        refs = await adapter.list_chunks("nonexistent")
        assert refs == []

    @patch("picard.adapters.i2c_adapter.asyncio.to_thread")
    async def test_list_after_write(self, mock_thread, adapter):
        mock_thread.return_value = None
        await adapter.write_chunk("PCHK:1:manifest1:0:0:1:dGVzdA==")
        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 1


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_check_success(self, adapter):
        with patch("picard.adapters.i2c_adapter.asyncio.to_thread", return_value=True):
            assert await adapter.health_check() is True

    async def test_health_check_failure(self, adapter):
        with patch("picard.adapters.i2c_adapter.asyncio.to_thread", side_effect=Exception("no device")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0
