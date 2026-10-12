"""Tests for the SoundCloud adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.soundcloud_adapter import SoundCloudAdapter, _silent_wav


def _make_adapter(**kwargs) -> SoundCloudAdapter:
    defaults = dict(client_id="cid", oauth_token="tok")
    defaults.update(kwargs)
    return SoundCloudAdapter(**defaults)


def _mock_resp(json_data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
class TestSoundCloudAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "soundcloud"
        assert p.max_chunk_bytes == 8000
        assert p.persistent is True

    def test_silent_wav(self):
        wav = _silent_wav()
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"
        assert len(wav) == 46

    async def test_write_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"id": 12345, "user_id": 67890})
        session = AsyncMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk("chunk-data")

        assert ref.adapter_name == "soundcloud"
        assert "12345" in ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({"description": "chunk-data"})
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="soundcloud", location="soundcloud://67890/12345")
            result = await a.read_chunk(ref)

        assert result == "chunk-data"

    async def test_delete_chunk(self):
        a = _make_adapter()
        resp = _mock_resp({}, status=204)
        session = AsyncMock()
        session.delete = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = StoredChunkRef(adapter_name="soundcloud", location="soundcloud://67890/12345")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        with patch("aiohttp.ClientSession", side_effect=Exception("err")):
            ref = StoredChunkRef(adapter_name="soundcloud", location="soundcloud://67890/12345")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        resp = _mock_resp([
            {"id": 1, "user_id": 99, "description": "PCHK:1:m1:data"},
            {"id": 2, "user_id": 99, "description": "PCHK:1:m2:data"},
        ])
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert len(refs) == 1

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        resp = _mock_resp([])
        session = AsyncMock()
        session.get = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
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

    async def test_headers(self):
        a = _make_adapter()
        h = a._headers()
        assert "OAuth tok" in h["Authorization"]
