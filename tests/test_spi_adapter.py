"""Tests for SPI flash adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.spi_adapter import SpiAdapter


@pytest.fixture
def adapter():
    return SpiAdapter(bus=0, device=0, speed_hz=1000000, flash_size=1048576)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "spi"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_write_returns_ref(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test123:0:0:hello")
        assert ref.adapter_name == "spi"
        assert ref.location.startswith("spi://")

    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_write_opaque_data(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("deadbeef0123456789")
        assert ref.adapter_name == "spi"

    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_offset_advances(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref1 = await adapter.write_chunk("PCHK:a:0:0:data1")
        ref2 = await adapter.write_chunk("PCHK:a:0:1:data2")
        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_delete_existing(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_delete_nonexistent(self, mock_thread, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="spi", location="spi://0.0/99999")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_list_by_manifest(self, mock_thread, adapter):
        mock_thread.return_value = None
        await adapter.write_chunk("PCHK:1:manifest1:0:0:1:dGVzdA==")
        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 1


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        with patch("picard.adapters.spi_adapter.asyncio.to_thread", return_value=True):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("picard.adapters.spi_adapter.asyncio.to_thread", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    @patch("picard.adapters.spi_adapter.asyncio.to_thread")
    async def test_capacity(self, mock_thread, adapter):
        mock_thread.return_value = None
        await adapter.write_chunk("PCHK:cap:0:0:data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["total_bytes"] == 1048576
        assert cap["free_bytes"] is not None
