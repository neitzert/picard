"""Tests for AX.25 packet radio adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.packet_radio import PacketRadioAdapter


@pytest.fixture
def adapter():
    return PacketRadioAdapter(callsign="N0CALL", dest_callsign="PICARD")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "packet-radio"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 256


class TestFrameBuilding:
    def test_encode_callsign_length(self, adapter):
        encoded = adapter._encode_callsign("N0CALL", ssid=0, last=False)
        assert len(encoded) == 7

    def test_kiss_escape_fend(self, adapter):
        data = bytes([0xC0])
        escaped = adapter._kiss_escape(data)
        assert escaped == bytes([0xDB, 0xDC])

    def test_kiss_escape_fesc(self, adapter):
        data = bytes([0xDB])
        escaped = adapter._kiss_escape(data)
        assert escaped == bytes([0xDB, 0xDD])

    def test_build_frame_has_fend(self, adapter):
        frame = adapter._build_frame(b"test")
        assert frame[0] == 0xC0
        assert frame[-1] == 0xC0


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:test:0:0:data")
        assert ref.adapter_name == "packet-radio"
        assert "ax25://" in ref.location

    async def test_write_caches_data(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:test:0:0:hello")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:hello"

    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="packet-radio", location="ax25://N0CALL/PICARD/999")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
