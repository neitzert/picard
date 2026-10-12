"""Tests for the WordPress adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.wordpress_adapter import WordPressAdapter


def _make_adapter(**kwargs) -> WordPressAdapter:
    defaults = dict(
        site_url="https://myblog.com",
        username="admin",
        application_password="app-pass",
    )
    defaults.update(kwargs)
    return WordPressAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestWordPressAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "wordpress"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"id": 42})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "wordpress"
        assert "42" in ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"content": {"raw": "chunk-data", "rendered": "<p>chunk-data</p>"}})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="wordpress", location="wp://https://myblog.com/42")
            result = await a.read_chunk(ref)

        assert result == "chunk-data"

    async def test_read_chunk_rendered_fallback(self):
        a = _make_adapter()
        resp = _mock_resp({"content": {"raw": "", "rendered": "<p>rendered</p>"}})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="wordpress", location="wp://https://myblog.com/42")
            result = await a.read_chunk(ref)

        assert result == "<p>rendered</p>"

    async def test_delete_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({}, status=200)
        session = AsyncMock()
        session.delete = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="wordpress", location="wp://https://myblog.com/42")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        with patch("aiohttp.ClientSession", side_effect=Exception("err")):
            ref = StoredChunkRef(adapter_name="wordpress", location="wp://https://myblog.com/42")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        resp = _mock_resp([
            {"id": 1, "content": {"raw": "PCHK:1:m1:data"}},
            {"id": 2, "content": {"raw": "PCHK:1:m2:data"}},
        ])
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

    async def test_api_url(self):
        a = _make_adapter()
        assert a._api_url("/posts") == "https://myblog.com/wp-json/wp/v2/posts"
        assert a._api_url("") == "https://myblog.com/wp-json/wp/v2"

    async def test_auth_header(self):
        a = _make_adapter()
        h = a._headers()
        assert h["Authorization"].startswith("Basic ")
