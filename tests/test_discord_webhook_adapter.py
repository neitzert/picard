"""Tests for the Discord Webhook adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.discord_webhook import DiscordWebhookAdapter


@pytest.fixture
def adapter():
    return DiscordWebhookAdapter(
        "https://discord.com/api/webhooks/123/abc-token",
        read_token="bot-test-token",
    )


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
        assert adapter.profile().name == "discord-webhook"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 6000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(200, {"id": "msg123"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("test chunk")

        assert ref.adapter_name == "discord-webhook"
        assert "msg123" in ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(200, {"id": "msg456"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached data")

        result = await adapter.read_chunk(ref)
        assert result == "cached data"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(200, {"id": "msg789"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello discord")
        assert await adapter.read_chunk(ref) == "hello discord"

    async def test_read_uncached_without_token_raises(self):
        a = DiscordWebhookAdapter("https://discord.com/api/webhooks/1/t")
        ref = StoredChunkRef(adapter_name="discord-webhook", location="discord-wh://1/msg1")
        with pytest.raises(RuntimeError, match="read requires"):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_via_webhook(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref_resp = _mock_response(200, {"id": "del1"})
            session2 = _mock_session(ref_resp)
            with patch("aiohttp.ClientSession", return_value=session2):
                ref = await adapter.write_chunk("deleteme")

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

    async def test_after_write(self, adapter):
        resp = _mock_response(200, {"id": "cap1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
