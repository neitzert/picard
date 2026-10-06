"""Tests for the Spotify adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.spotify_adapter import SpotifyAdapter


def _make_adapter(**kwargs) -> SpotifyAdapter:
    defaults = dict(
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken",
        user_id="user123",
    )
    defaults.update(kwargs)
    return SpotifyAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(responses):
    session = AsyncMock()
    session.post = MagicMock(side_effect=responses[:])
    session.get = MagicMock(side_effect=responses[:])
    session.delete = MagicMock(side_effect=responses[:])
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
class TestSpotifyAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "spotify"
        assert p.max_chunk_bytes == 300
        assert p.persistent is True
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        a._access_token = "tok"
        create_resp = _mock_resp({"id": "playlist-abc"})
        session = AsyncMock()
        session.post = MagicMock(return_value=create_resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("test-chunk-data")

        assert ref.adapter_name == "spotify"
        assert "playlist-abc" in ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({"description": "my-chunk-data"})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="spotify", location="spotify://user123/pl-1")
            result = await a.read_chunk(ref)

        assert result == "my-chunk-data"

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({}, status=200)
        session = AsyncMock()
        session.delete = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="spotify", location="spotify://user123/pl-1")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({}, status=404)
        session = AsyncMock()
        session.delete = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="spotify", location="spotify://user123/pl-1")
            assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._access_token = "tok"

        with patch("aiohttp.ClientSession", side_effect=Exception("boom")):
            ref = StoredChunkRef(adapter_name="spotify", location="spotify://user123/pl-1")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({
            "items": [
                {"id": "pl-1", "description": "PCHK:1:m1:data"},
                {"id": "pl-2", "description": "PCHK:1:m2:data"},
            ],
            "next": None,
        })
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert len(refs) == 1
        assert "pl-1" in refs[0].location

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({"items": [], "next": None})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._access_token = "tok"
        resp = _mock_resp({}, status=200)
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._access_token = "tok"

        with patch("aiohttp.ClientSession", side_effect=Exception("timeout")):
            assert await a.health_check() is False

    async def test_ensure_token(self):
        a = _make_adapter()
        resp = _mock_resp({"access_token": "new-tok"})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)

        await a._ensure_token(session)
        assert a._access_token == "new-tok"
