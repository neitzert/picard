"""Tests for the Slack Webhook adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.slack_webhook import SlackWebhookAdapter


@pytest.fixture
def adapter():
    return SlackWebhookAdapter(
        "https://hooks.slack.com/services/T00/B00/xxxx",
        channel="#test",
        read_api_token="xoxb-test",
    )


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value="ok")
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
        assert adapter.profile().name == "slack-webhook"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 40_000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("test chunk data")

        assert ref.adapter_name == "slack-webhook"
        assert "slack-wh://" in ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached data")

        result = await adapter.read_chunk(ref)
        assert result == "cached data"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello")
        result = await adapter.read_chunk(ref)
        assert result == "hello"

    async def test_read_uncached_without_token_raises(self):
        adapter = SlackWebhookAdapter("https://hooks.slack.com/x")
        ref = StoredChunkRef(adapter_name="slack-webhook", location="slack-wh://default/abc")
        with pytest.raises(RuntimeError, match="read requires"):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_removes_from_cache(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("deleteme")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("conn err")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_after_write(self, adapter):
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
