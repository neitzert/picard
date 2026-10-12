"""Tests for the ntfy adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ntfy_adapter import NtfyAdapter


@pytest.fixture
def adapter():
    return NtfyAdapter("picard-test-topic")


def _mock_response(status=200, json_data=None, text=""):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
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
        assert adapter.profile().name == "ntfy"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60

    def test_ttl(self, adapter):
        assert adapter.profile().ttl_seconds == 43200


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        resp = _mock_response(200, {"id": "ntfy-abc123"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("ntfy chunk")

        assert ref.adapter_name == "ntfy"
        assert "ntfy://" in ref.location
        assert "ntfy-abc123" in ref.location

    async def test_write_caches(self, adapter):
        resp = _mock_response(200, {"id": "ntfy-xyz"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("cached ntfy")

        assert await adapter.read_chunk(ref) == "cached ntfy"


@pytest.mark.asyncio
class TestRead:
    async def test_read_from_cache(self, adapter):
        resp = _mock_response(200, {"id": "r1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("hello ntfy")
        assert await adapter.read_chunk(ref) == "hello ntfy"

    async def test_read_from_api(self, adapter):
        import json

        poll_text = json.dumps({"id": "remote1", "message": "remote data"})
        ref = StoredChunkRef(
            adapter_name="ntfy",
            location="ntfy://https://ntfy.sh/picard-test-topic/remote1",
        )
        resp = _mock_response(200, text=poll_text)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await adapter.read_chunk(ref)
        assert result == "remote data"

    async def test_read_not_found(self, adapter):
        ref = StoredChunkRef(
            adapter_name="ntfy",
            location="ntfy://https://ntfy.sh/picard-test-topic/missing",
        )
        resp = _mock_response(200, text="")
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_cached(self, adapter):
        resp = _mock_response(200, {"id": "del1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("del")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_uncached(self, adapter):
        ref = StoredChunkRef(adapter_name="ntfy", location="ntfy://x/nope")
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

    async def test_after_write(self, adapter):
        resp = _mock_response(200, {"id": "cap1"})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
