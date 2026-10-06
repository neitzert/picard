"""Tests for the Flickr adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.flickr_adapter import FlickrAdapter, _tiny_png


def _make_adapter(**kwargs) -> FlickrAdapter:
    defaults = dict(
        api_key="ak", api_secret="as",
        oauth_token="ot", oauth_secret="os",
    )
    defaults.update(kwargs)
    return FlickrAdapter(**defaults)


def _mock_resp(json_data=None, text_data=None, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestFlickrAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "flickr"
        assert p.max_chunk_bytes == 32768
        assert p.persistent is True

    def test_tiny_png(self):
        png = _tiny_png()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(png) > 30

    async def test_write_chunk(self):
        a = _make_adapter()
        xml_resp = '<rsp stat="ok"><photoid>12345</photoid></rsp>'
        resp = _mock_resp(text_data=xml_resp)
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "flickr"
        assert "12345" in ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        resp = _mock_resp(json_data={
            "photo": {"description": {"_content": "chunk-data"}}
        })
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="flickr", location="flickr://12345")
            result = await a.read_chunk(ref)

        assert result == "chunk-data"

    async def test_delete_chunk(self):
        a = _make_adapter()
        resp = _mock_resp(json_data={}, status=200)
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="flickr", location="flickr://12345")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        with patch("aiohttp.ClientSession", side_effect=Exception("err")):
            ref = StoredChunkRef(adapter_name="flickr", location="flickr://12345")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        resp = _mock_resp(json_data={
            "photos": {"photo": [
                {"id": "1", "description": {"_content": "PCHK:1:m1:data"}},
                {"id": "2", "description": {"_content": "PCHK:1:m2:data"}},
            ]}
        })
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert len(refs) == 1

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        resp = _mock_resp(json_data={"photos": {"photo": []}})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = _mock_resp(json_data={}, status=200)
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

    async def test_auth_params(self):
        a = _make_adapter()
        params = a._auth_params()
        assert params["api_key"] == "ak"
        assert params["format"] == "json"
