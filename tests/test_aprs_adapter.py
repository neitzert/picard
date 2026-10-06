"""Tests for APRS ham radio adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.aprs_adapter import AprsAdapter


@pytest.fixture
def adapter():
    return AprsAdapter(callsign="N0CALL", passcode="12345")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "aprs"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 6

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 67


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"# server banner\r\n")
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:test:0:0:hi")
        assert ref.adapter_name == "aprs"
        assert ref.location.startswith("aprs://")

    async def test_write_caches_for_read(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"# banner\r\n")
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:test:0:0:hello")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:hello"

    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="aprs", location="aprs://N0CALL/999")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"# banner\r\n")
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)):
            ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="aprs", location="aprs://N0CALL/999")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        mock_writer = AsyncMock()
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"# aprsc 2.1.4\r\n")
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
        assert cap["used_bytes"] == 0
