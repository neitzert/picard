"""Tests for UART/serial adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.uart_adapter import UartAdapter


@pytest.fixture
def adapter():
    return UartAdapter(port="/dev/ttyUSB0", baudrate=115200)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "uart"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_write_returns_ref(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test:0:0:data")
        assert ref.adapter_name == "uart"
        assert ref.location.startswith("uart://")

    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_write_caches_data(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test:0:0:hello")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:hello"

    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_write_opaque_data(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("deadbeef")
        assert ref.adapter_name == "uart"

    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_frame_counter_increments(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref1 = await adapter.write_chunk("PCHK:a:0:0:d1")
        ref2 = await adapter.write_chunk("PCHK:a:0:1:d2")
        assert ref1.location != ref2.location


class TestCrc:
    def test_crc16_deterministic(self):
        data = b"hello"
        assert UartAdapter._crc16(data) == UartAdapter._crc16(data)

    def test_crc16_different_data(self):
        assert UartAdapter._crc16(b"hello") != UartAdapter._crc16(b"world")


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_delete_existing(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_delete_nonexistent(self, mock_thread, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="uart", location="uart:///dev/ttyUSB0/999")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        with patch("picard.adapters.uart_adapter.asyncio.to_thread", return_value=True):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("picard.adapters.uart_adapter.asyncio.to_thread", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    @patch("picard.adapters.uart_adapter.asyncio.to_thread")
    async def test_capacity_empty(self, mock_thread, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
