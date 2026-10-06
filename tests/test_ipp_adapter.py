"""Tests for the IPP (Internet Printing Protocol) adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.ipp_adapter import IppAdapter


@pytest.fixture
def adapter():
    return IppAdapter(printer_url="ipp://printer.local:631/ipp/print")


def _mock_session(status=200):
    mock_resp = AsyncMock()
    mock_resp.read = AsyncMock(return_value=b"\x02\x00\x00\x00\x00\x00\x00\x01\x03")
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = AsyncMock()
    mock_sess.post = MagicMock(return_value=mock_resp)
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)
    return mock_sess


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "ipp"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1024


@pytest.mark.asyncio
class TestWriteRead:
    @patch("aiohttp.ClientSession")
    async def test_round_trip(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session()
        ref = await adapter.write_chunk("print-job-data")
        assert ref.adapter_name == "ipp"
        result = await adapter.read_chunk(ref)
        assert result == "print-job-data"

    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="ipp", location="ipp://missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    @patch("aiohttp.ClientSession")
    async def test_delete(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session()
        ref = await adapter.write_chunk("del-print")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False


class TestBuildIppRequest:
    def test_request_format(self, adapter):
        data = adapter._build_ipp_request(0x000B, {"attributes-charset": "utf-8"})
        assert data[:2] == b"\x02\x00"
        assert data[-1:] == b"\x03"


@pytest.mark.asyncio
class TestList:
    @patch("aiohttp.ClientSession")
    async def test_list_chunks(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session()
        await adapter.write_chunk("PCHK:1:man1:0:0:1:ZGF0YTE=")
        refs = await adapter.list_chunks("man1")
        assert len(refs) >= 1


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0


@pytest.mark.asyncio
class TestHealthCheck:
    @patch("aiohttp.ClientSession")
    async def test_healthy(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session(status=200)
        assert await adapter.health_check() is True
