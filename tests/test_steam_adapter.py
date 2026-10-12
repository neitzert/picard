"""Tests for the Steam adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.steam_adapter import SteamAdapter


@pytest.fixture
def adapter():
    return SteamAdapter(
        api_key="fake-key",
        steam_id="76561198000000001",
        session_id="fake-session",
        login_cookie="fake-cookie",
    )


def _mock_session(json_data=None, status=200):
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_sess = AsyncMock()
    mock_sess.post = MagicMock(return_value=mock_resp)
    mock_sess.get = MagicMock(return_value=mock_resp)
    mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
    mock_sess.__aexit__ = AsyncMock(return_value=False)
    return mock_sess


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "steam"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 15

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8000


@pytest.mark.asyncio
class TestWriteRead:
    @patch("aiohttp.ClientSession")
    async def test_round_trip(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session({"gidcomment": "12345"})
        ref = await adapter.write_chunk("steam-data")
        assert ref.adapter_name == "steam"
        result = await adapter.read_chunk(ref)
        assert result == "steam-data"

    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="steam", location="steam://miss/0")
        with pytest.raises((KeyError, Exception)):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    @patch("aiohttp.ClientSession")
    async def test_delete(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session({"gidcomment": "99"})
        ref = await adapter.write_chunk("del-steam")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    @patch("aiohttp.ClientSession")
    async def test_list(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session({"gidcomment": "1"})
        await adapter.write_chunk("PCHK:1:man1:0:0:1:ZGF0YTE=")
        refs = await adapter.list_chunks("man1")
        assert len(refs) >= 1


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0


@pytest.mark.asyncio
class TestHealthCheck:
    @patch("aiohttp.ClientSession")
    async def test_healthy(self, mock_cls, adapter):
        mock_cls.return_value = _mock_session(status=200)
        assert await adapter.health_check() is True
