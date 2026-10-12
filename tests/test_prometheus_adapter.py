"""Tests for the Prometheus Pushgateway adapter."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.prometheus_adapter import PrometheusAdapter


def _mock_aiohttp(resp_mock):
    """Build a mock aiohttp module with a ClientSession that returns resp_mock."""
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.put = MagicMock(return_value=resp_mock)
    mock_session.get = MagicMock(return_value=resp_mock)
    mock_session.delete = MagicMock(return_value=resp_mock)

    mock_aiohttp = MagicMock()
    mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)
    return mock_aiohttp


def _ok_resp(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.fixture
def adapter():
    return PrometheusAdapter(
        "http://pushgw:9091",
        job_name="picard",
        query_url="http://prometheus:9090",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "prometheus"

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 300


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock = _mock_aiohttp(_ok_resp(200))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("PCHK:1:prom1:0:0:1:payload")
            assert ref.adapter_name == "prometheus"
            assert ref.location.startswith("prom://picard/")

    async def test_location_encodes_chunk_info(self, adapter):
        mock = _mock_aiohttp(_ok_resp(200))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("PCHK:1:abc:2:0:1:data")
            assert "abc" in ref.location

    async def test_opaque_data(self, adapter):
        mock = _mock_aiohttp(_ok_resp(200))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = await adapter.write_chunk("sealed-data")
            assert ref.adapter_name == "prometheus"


@pytest.mark.asyncio
class TestReadNoQueryUrl:
    async def test_read_without_query_url_raises(self):
        a = PrometheusAdapter("http://pushgw:9091")
        ref = StoredChunkRef(adapter_name="prometheus", location="prom://picard/abc/0_0")
        with pytest.raises(LookupError, match="query_url"):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete(self, adapter):
        mock = _mock_aiohttp(_ok_resp(202))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = StoredChunkRef(
                adapter_name="prometheus",
                location="prom://picard/abc/0_0",
            )
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter):
        mock = _mock_aiohttp(_ok_resp(404))
        with patch.dict(sys.modules, {"aiohttp": mock}):
            ref = StoredChunkRef(
                adapter_name="prometheus",
                location="prom://picard/abc/0_0",
            )
            assert await adapter.delete_chunk(ref) is False


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


@pytest.mark.asyncio
class TestListNoQueryUrl:
    async def test_list_without_query_url(self):
        a = PrometheusAdapter("http://pushgw:9091")
        refs = await a.list_chunks("abc")
        assert refs == []
