"""Tests for the TikTok adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tiktok_adapter import TikTokAdapter


def _make_adapter(**kwargs) -> TikTokAdapter:
    defaults = dict(access_token="tok", open_id="oid123")
    defaults.update(kwargs)
    return TikTokAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestTikTokAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "tiktok"
        assert p.max_chunk_bytes == 2200
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"data": {"publish_id": "pub-1"}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "tiktok"
        assert "pub-1" in ref.location
        assert a._cache[ref.location] == "chunk-data"

    async def test_read_chunk_cached(self):
        a = _make_adapter()
        loc = "tiktok://oid123/pub-1"
        a._cache[loc] = "cached-data"

        ref = StoredChunkRef(adapter_name="tiktok", location=loc)
        result = await a.read_chunk(ref)
        assert result == "cached-data"

    async def test_read_chunk_api(self):
        a = _make_adapter()
        resp = _mock_resp({"data": {"videos": [{"title": "api-data"}]}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="tiktok", location="tiktok://oid123/vid-1")
            result = await a.read_chunk(ref)

        assert result == "api-data"

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        resp = _mock_resp({"data": {"videos": []}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="tiktok", location="tiktok://oid123/nope")
            with pytest.raises(KeyError):
                await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        loc = "tiktok://oid123/pub-1"
        a._cache[loc] = "data"

        ref = StoredChunkRef(adapter_name="tiktok", location=loc)
        assert await a.delete_chunk(ref) is True
        assert loc not in a._cache

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="tiktok", location="tiktok://oid123/nope")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._cache["tiktok://oid123/1"] = "PCHK:1:m1:data"
        a._cache["tiktok://oid123/2"] = "PCHK:1:m2:data"

        refs = await a.list_chunks("m1")
        assert len(refs) == 1

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = _mock_resp({}, status=200)
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        with patch("aiohttp.ClientSession", side_effect=Exception("err")):
            assert await a.health_check() is False
