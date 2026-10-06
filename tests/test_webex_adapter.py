"""Tests for the Cisco Webex adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.webex_adapter import WebexAdapter


@pytest.fixture
def adapter():
    return WebexAdapter("test-token", "room-123")


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
    session.delete = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "webex"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 7439

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(200, {"id": "msg-webex-1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("webex chunk")

        assert ref.adapter_name == "webex"
        assert "webex://room-123/msg-webex-1" == ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(200, {"id": "msg-webex-2"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached webex")

        assert await adapter.read_chunk(ref) == "cached webex"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(200, {"id": "msg-r1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("read me")
        assert await adapter.read_chunk(ref) == "read me"

    async def test_read_from_api(self, adapter):
        ref = StoredChunkRef(adapter_name="webex", location="webex://room-123/msg-remote")
        resp = _mock_response(200, {"markdown": "PICARD:abc\nremote data"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await adapter.read_chunk(ref)
        assert result == "remote data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_ok(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            wr = _mock_response(200, {"id": "del1"})
            ws = _mock_session(wr)
            with patch("aiohttp.ClientSession", return_value=ws):
                ref = await adapter.write_chunk("del")

        with patch("aiohttp.ClientSession", return_value=session):
            assert await adapter.delete_chunk(ref) is True


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
        assert cap["used_bytes"] == 0
