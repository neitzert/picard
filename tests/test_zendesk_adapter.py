"""Tests for the Zendesk adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.zendesk_adapter import ZendeskAdapter


@pytest.fixture
def adapter():
    return ZendeskAdapter("testco", "agent@example.com", "zd-api-token")


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
        assert adapter.profile().name == "zendesk"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(201, {"ticket": {"id": 42}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("zendesk ticket data")

        assert ref.adapter_name == "zendesk"
        assert "zendesk://testco/tickets/42" == ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(201, {"ticket": {"id": 99}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached zd data")

        assert await adapter.read_chunk(ref) == "cached zd data"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(201, {"ticket": {"id": 10}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello zd")
        assert await adapter.read_chunk(ref) == "hello zd"

    async def test_read_uncached_raises(self, adapter):
        ref = StoredChunkRef(
            adapter_name="zendesk", location="zendesk://x/tickets/nope"
        )
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_cached(self, adapter):
        resp = _mock_response(201, {"ticket": {"id": 77}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("del")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_uncached(self, adapter):
        ref = StoredChunkRef(
            adapter_name="zendesk", location="zendesk://x/tickets/nope"
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        chunks = await adapter.list_chunks("manifest-1")
        assert len(chunks) == 0

    async def test_list_after_write(self, adapter):
        resp = _mock_response(201, {"ticket": {"id": 88}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("listed")
        chunks = await adapter.list_chunks("manifest-1")
        assert len(chunks) == 1


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
        resp = _mock_response(201, {"ticket": {"id": 55}})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
