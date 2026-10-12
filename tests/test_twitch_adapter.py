"""Tests for the Twitch adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.twitch_adapter import TwitchAdapter


def _make_adapter(**kwargs) -> TwitchAdapter:
    defaults = dict(client_id="cid", access_token="tok", broadcaster_id="bc123")
    defaults.update(kwargs)
    return TwitchAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestTwitchAdapter:
    async def test_profile_description_mode(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "twitch"
        assert p.persistent is True

    async def test_profile_chat_mode(self):
        a = _make_adapter(use_chat=True)
        p = a.profile()
        assert p.persistent is False

    async def test_write_chunk_description(self):
        a = _make_adapter()
        resp = _mock_resp({}, status=204)
        session = AsyncMock()
        session.patch = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "twitch"
        assert "description" in ref.location

    async def test_write_chunk_chat(self):
        a = _make_adapter(use_chat=True)
        resp = _mock_resp({"data": [{"message_id": "msg-1"}]})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert "chat" in ref.location
        assert a._chat_cache[ref.location] == "chunk-data"

    async def test_read_chunk_description(self):
        a = _make_adapter()
        resp = _mock_resp({"data": [{"description": "chunk-data"}]})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="twitch", location="twitch://bc123/description")
            result = await a.read_chunk(ref)

        assert result == "chunk-data"

    async def test_read_chunk_chat_cached(self):
        a = _make_adapter(use_chat=True)
        loc = "twitch://bc123/chat/msg-1"
        a._chat_cache[loc] = "cached-data"

        ref = StoredChunkRef(adapter_name="twitch", location=loc)
        result = await a.read_chunk(ref)
        assert result == "cached-data"

    async def test_read_chunk_chat_missing(self):
        a = _make_adapter(use_chat=True)
        ref = StoredChunkRef(adapter_name="twitch", location="twitch://bc123/chat/nope")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)

    async def test_delete_chunk_chat(self):
        a = _make_adapter(use_chat=True)
        loc = "twitch://bc123/chat/msg-1"
        a._chat_cache[loc] = "data"

        ref = StoredChunkRef(adapter_name="twitch", location=loc)
        assert await a.delete_chunk(ref) is True
        assert loc not in a._chat_cache

    async def test_list_chunks_chat(self):
        a = _make_adapter(use_chat=True)
        a._chat_cache["twitch://bc123/chat/1"] = "PCHK:1:m1:data"
        a._chat_cache["twitch://bc123/chat/2"] = "PCHK:1:m2:data"

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
