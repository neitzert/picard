"""Tests for the InfluxDB adapter."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.influxdb_adapter import (
    InfluxdbAdapter,
    _extract_csv_value,
    _parse_chunk_list,
)


def _mock_aiohttp(resp_mock):
    """Build a mock aiohttp module with a ClientSession that returns resp_mock."""
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=resp_mock)
    mock_session.get = MagicMock(return_value=resp_mock)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    return mock_aiohttp


def _ok_resp(status=204, text_data=""):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.fixture
def adapter():
    return InfluxdbAdapter(
        url="http://influx:8086",
        token="test-token",
        org="myorg",
        bucket="picard",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "influxdb"

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65535

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock = _mock_aiohttp(_ok_resp(204))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("PCHK:1:inf1:0:0:1:payload")
            assert ref.adapter_name == "influxdb"
            assert ref.location.startswith("influx://picard/")

    async def test_write_encodes_location(self, adapter):
        mock = _mock_aiohttp(_ok_resp(204))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("PCHK:1:abc:3:0:1:data")
            assert "abc" in ref.location

    async def test_write_opaque_data(self, adapter):
        mock = _mock_aiohttp(_ok_resp(204))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("sealed-hex-data")
            assert ref.adapter_name == "influxdb"

    async def test_write_error_raises(self, adapter):
        mock = _mock_aiohttp(_ok_resp(500, "server error"))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            with pytest.raises(IOError, match="influxdb write failed"):
                await adapter.write_chunk("PCHK:1:err:0:0:1:data")


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock = _mock_aiohttp(_ok_resp(204))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = StoredChunkRef(
                adapter_name="influxdb",
                location="influx://picard/abc/0_0",
            )
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock = _mock_aiohttp(_ok_resp(200))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_aiohttp = MagicMock()
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(side_effect=OSError("down"))
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
        with patch.dict(sys.modules, {"aiohttp": mock_aiohttp}):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_returns_none(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] is None
        assert cap["chunk_count"] is None


class TestExtractCsvValue:
    def test_basic(self):
        csv = "#comment\n,result,table,_time,_value\n,_result,0,2026-01-01T00:00:00Z,dGVzdA=="
        assert _extract_csv_value(csv) == "dGVzdA=="

    def test_no_data_raises(self):
        with pytest.raises(KeyError):
            _extract_csv_value("")

    def test_missing_column_raises(self):
        csv = "#comment\n,result,table,_time\n,_result,0,2026-01-01T00:00:00Z"
        with pytest.raises(KeyError):
            _extract_csv_value(csv)


class TestParseChunkList:
    def test_basic(self):
        csv = "#comment\n,block_index,chunk_index\n,0,0\n,0,1\n,1,0"
        refs = _parse_chunk_list(csv, "picard", "abc")
        assert len(refs) == 3
        assert refs[0].location == "influx://picard/abc/0_0"

    def test_empty(self):
        refs = _parse_chunk_list("", "picard", "abc")
        assert refs == []
