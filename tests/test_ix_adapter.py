"""Tests for the ix.io adapter -- mocked, no real API calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ix_adapter import IxAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> IxAdapter:
    return IxAdapter(**kwargs)


def _mock_response(status=200, json_data=None, text_data=""):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)
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


@pytest.mark.asyncio
class TestIxAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ix-io"
        assert p.max_chunk_bytes == 524_288
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_profile_custom_max(self):
        a = _make_adapter(max_chunk_bytes=100_000)
        p = a.profile()
        assert p.max_chunk_bytes == 100_000

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, text_data="http://ix.io/abc123\n")
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ix-io"
        assert ref.location == "ix://abc123"
        session.post.assert_called_once()

    async def test_write_chunk_trailing_slash(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, text_data="http://ix.io/xyz789/\n")
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.location == "ix://xyz789"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, text_data=enc)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="ix-io", location="ix://abc123")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.read_chunk(ref)

        assert result == enc
        session.get.assert_called_once()

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="ix-io", location="ix://abc123")
        result = await a.delete_chunk(ref)
        assert result is False

    async def test_list_chunks_always_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.health_check()

        assert result is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get.side_effect = Exception("network error")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.health_check()

        assert result is False

    async def test_custom_url(self):
        a = _make_adapter(url="http://custom-ix.example.com/")
        assert a._url == "http://custom-ix.example.com"
        p = a.profile()
        assert p.name == "ix-io"

    async def test_write_chunk_post_data(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, text_data="http://ix.io/test1\n")
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            await a.write_chunk(enc)

        call_kwargs = session.post.call_args
        assert call_kwargs[0][0] == "https://ix.io"
        assert call_kwargs[1]["data"] == {"f:1": enc}
