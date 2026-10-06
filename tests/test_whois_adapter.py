"""Tests for the WHOIS adapter."""

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.whois_adapter import WhoisAdapter


@pytest.fixture
def adapter():
    return WhoisAdapter("whois.example.com")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "whois"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"OK\r\n")
        mock_writer = AsyncMock()
        mock_writer.close = AsyncMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            ref = await adapter.write_chunk("PCHK:man1:0:0:hello")

        assert ref.adapter_name == "whois"
        assert "whois://whois.example.com/PCHK-" in ref.location


@pytest.mark.asyncio
class TestRead:
    async def test_read_extracts_remarks(self, adapter):
        whois_response = (
            "descr: PCHK-man1-0-0\n"
            "remarks: PCHK:man1:0:0:hello\n"
            "source: TEST\n"
        )
        mock_reader = AsyncMock()
        mock_reader.read = AsyncMock(return_value=whois_response.encode())
        mock_writer = AsyncMock()
        mock_writer.close = AsyncMock()
        mock_writer.wait_closed = AsyncMock()

        ref = StoredChunkRef(
            adapter_name="whois",
            location="whois://whois.example.com/PCHK-man1-0-0",
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            result = await adapter.read_chunk(ref)

        assert result == "PCHK:man1:0:0:hello"

    async def test_read_not_found(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.read = AsyncMock(return_value=b"% No entries found\n")
        mock_writer = AsyncMock()
        mock_writer.close = AsyncMock()
        mock_writer.wait_closed = AsyncMock()

        ref = StoredChunkRef(
            adapter_name="whois",
            location="whois://whois.example.com/PCHK-nope",
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_no_write_url(self, adapter):
        ref = StoredChunkRef(
            adapter_name="whois",
            location="whois://whois.example.com/PCHK-test",
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.read = AsyncMock(return_value=b"% WHOIS server\n")
        mock_writer = AsyncMock()
        mock_writer.close = AsyncMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("asyncio.open_connection", AsyncMock(side_effect=Exception)):
            assert await adapter.health_check() is False
