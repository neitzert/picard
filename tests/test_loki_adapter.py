"""Tests for the Grafana Loki adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.loki_adapter import LokiAdapter


@pytest.fixture
def adapter():
    return LokiAdapter("http://loki.example.com:3100")


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
        assert adapter.profile().name == "loki"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8192

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 120


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("loki log data")

        assert ref.adapter_name == "loki"
        assert ref.location.startswith("loki://")

    async def test_write_caches(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached loki data")

        assert await adapter.read_chunk(ref) == "cached loki data"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello loki")
        assert await adapter.read_chunk(ref) == "hello loki"

    async def test_read_uncached_raises(self, adapter):
        ref = StoredChunkRef(adapter_name="loki", location="loki://nope@0")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_cached(self, adapter):
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("del")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_uncached(self, adapter):
        ref = StoredChunkRef(adapter_name="loki", location="loki://nope@0")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        chunks = await adapter.list_chunks("manifest-1")
        assert len(chunks) == 0

    async def test_list_after_write(self, adapter):
        resp = _mock_response(204)
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
        resp = _mock_response(204)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
