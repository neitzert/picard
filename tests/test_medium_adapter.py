"""Tests for the Medium adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.medium_adapter import MediumAdapter


def _make_adapter(**kwargs) -> MediumAdapter:
    defaults = dict(integration_token="tok", author_id="auth123")
    defaults.update(kwargs)
    return MediumAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestMediumAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "medium"
        assert p.max_chunk_bytes == 65536
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"data": {"id": "post-1"}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "medium"
        assert "post-1" in ref.location
        assert a._cache[ref.location] == "chunk-data"

    async def test_read_chunk_cached(self):
        a = _make_adapter()
        loc = "medium://auth123/post-1"
        a._cache[loc] = "cached-data"

        ref = StoredChunkRef(adapter_name="medium", location=loc)
        result = await a.read_chunk(ref)
        assert result == "cached-data"

    async def test_read_chunk_not_cached(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="medium", location="medium://auth123/nope")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        loc = "medium://auth123/post-1"
        a._cache[loc] = "data"

        ref = StoredChunkRef(adapter_name="medium", location=loc)
        assert await a.delete_chunk(ref) is True
        assert loc not in a._cache

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="medium", location="medium://auth123/nope")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._cache["medium://auth123/1"] = "PCHK:1:m1:data"
        a._cache["medium://auth123/2"] = "PCHK:1:m2:data"

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

    async def test_capacity(self):
        a = _make_adapter()
        a._cache["loc1"] = "abc"
        a._cache["loc2"] = "defgh"
        cap = await a.capacity()
        assert cap["chunk_count"] == 2
        assert cap["used_bytes"] == 8
        assert cap["free_bytes"] is None
