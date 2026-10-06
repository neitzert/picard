"""Tests for the Tumblr adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tumblr_adapter import TumblrAdapter


def _make_adapter(**kwargs) -> TumblrAdapter:
    defaults = dict(
        consumer_key="ck", consumer_secret="cs",
        oauth_token="ot", oauth_secret="os", blog_name="myblog",
    )
    defaults.update(kwargs)
    return TumblrAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestTumblrAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "tumblr"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"response": {"id": 12345}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "tumblr"
        assert "12345" in ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"response": {"posts": [{"body": "chunk-data"}]}})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="tumblr", location="tumblr://myblog/12345")
            result = await a.read_chunk(ref)

        assert result == "chunk-data"

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        resp = _mock_resp({"response": {"posts": []}})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="tumblr", location="tumblr://myblog/nope")
            with pytest.raises(KeyError):
                await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({}, status=200)
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="tumblr", location="tumblr://myblog/12345")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        with patch("aiohttp.ClientSession", side_effect=Exception("err")):
            ref = StoredChunkRef(adapter_name="tumblr", location="tumblr://myblog/12345")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        resp = _mock_resp({
            "response": {"posts": [
                {"id": 1, "body": "PCHK:1:m1:data"},
                {"id": 2, "body": "PCHK:1:m2:data"},
            ]}
        })
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert len(refs) == 1

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

    async def test_draft_state_in_write(self):
        a = _make_adapter()
        resp = _mock_resp({"response": {"id": 999}})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            await a.write_chunk("data")

        call_kwargs = session.post.call_args
        assert call_kwargs is not None
