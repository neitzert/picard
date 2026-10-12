"""Tests for the finger adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.finger_adapter import FingerAdapter


@pytest.fixture
def adapter():
    return FingerAdapter("finger.example.com")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "finger"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"OK\r\n")
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            ref = await adapter.write_chunk("PCHK:1:man1:0:0:1:aGVsbG8=")

        assert ref.adapter_name == "finger"
        assert "finger://finger.example.com/" in ref.location
        assert "man1.0.0" in ref.location

    async def test_write_opaque_data(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"OK\r\n")
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            ref = await adapter.write_chunk("not-a-pchk-format")

        assert ref.adapter_name == "finger"
        assert "finger://" in ref.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_returns_data(self, adapter):
        chunk_data = "PCHK:1:man1:0:0:1:aGVsbG8="
        mock_reader = AsyncMock()
        mock_reader.read = AsyncMock(return_value=chunk_data.encode())
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        ref = StoredChunkRef(
            adapter_name="finger",
            location="finger://finger.example.com/man1.0.0",
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            result = await adapter.read_chunk(ref)

        assert result == chunk_data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"OK\r\n")
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        ref = StoredChunkRef(
            adapter_name="finger",
            location="finger://finger.example.com/key1",
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_fail(self, adapter):
        ref = StoredChunkRef(
            adapter_name="finger",
            location="finger://finger.example.com/key1",
        )

        with patch("asyncio.open_connection", AsyncMock(side_effect=Exception)):
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"finger server\r\n")
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("asyncio.open_connection", AsyncMock(side_effect=Exception)):
            assert await adapter.health_check() is False
