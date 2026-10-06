"""Tests for the Signal adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.signal_adapter import SignalAdapter


@pytest.fixture
def adapter():
    return SignalAdapter("+1234567890", "+0987654321", api_url="http://localhost:8080")


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp):
    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "signal"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(200, {"timestamp": "1696600000"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("signal chunk")

        assert ref.adapter_name == "signal"
        assert "signal://+1234567890/" in ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(200, {"timestamp": "1696600001"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("e2e encrypted")

        assert await adapter.read_chunk(ref) == "e2e encrypted"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(200, {"timestamp": "ts1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello signal")
        assert await adapter.read_chunk(ref) == "hello signal"

    async def test_read_uncached_not_found(self, adapter):
        resp = _mock_response(200, [])
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="signal", location="signal://+1234567890/missing")
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_cached(self, adapter):
        resp = _mock_response(200, {"timestamp": "ts-del"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("del")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_uncached(self, adapter):
        ref = StoredChunkRef(adapter_name="signal", location="signal://x/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
