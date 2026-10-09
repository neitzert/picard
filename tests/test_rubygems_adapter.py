"""Tests for the RubyGems adapter -- mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.rubygems_adapter import RubyGemsAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kw) -> RubyGemsAdapter:
    defaults = dict(api_key="test-key", registry_url="https://rubygems.example.com")
    defaults.update(kw)
    return RubyGemsAdapter(**defaults)


def _mock_session(method="get", status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    setattr(session, method, MagicMock(return_value=cm))
    session.closed = False
    return session, session


@pytest.mark.asyncio
class TestRubyGemsProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "rubygems"
        assert p.max_chunk_bytes == 32768
        assert p.persistent is True


@pytest.mark.asyncio
class TestRubyGemsWrite:
    @patch("aiohttp.ClientSession")
    async def test_write_chunk(self, MockSession):
        session_cm, _ = _mock_session("post")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk(_sample_encoded())
        assert ref.adapter_name == "rubygems"
        assert ref.location.startswith("gems://")

    @patch("aiohttp.ClientSession")
    async def test_write_opaque(self, MockSession):
        session_cm, _ = _mock_session("post")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk("opaque-data")
        assert ref.adapter_name == "rubygems"


@pytest.mark.asyncio
class TestRubyGemsRead:
    @patch("aiohttp.ClientSession")
    async def test_read_chunk(self, MockSession):
        enc = _sample_encoded()
        session_cm, _ = _mock_session("get", json_data={"info": enc})
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="rubygems", location="gems://picard-chunk-abc/0.0.1")
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("aiohttp.ClientSession")
    async def test_read_not_found(self, MockSession):
        session_cm, _ = _mock_session("get", json_data={"info": ""})
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="rubygems", location="gems://missing/0.0.1")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestRubyGemsDelete:
    @patch("aiohttp.ClientSession")
    async def test_delete_success(self, MockSession):
        session_cm, _ = _mock_session("delete")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="rubygems", location="gems://picard-chunk-abc/0.0.1")
        assert await a.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestRubyGemsHealth:
    @patch("aiohttp.ClientSession")
    async def test_health_ok(self, MockSession):
        session_cm, _ = _mock_session("get")
        MockSession.return_value = session_cm
        a = _make_adapter()
        assert await a.health_check() is True

    @patch("aiohttp.ClientSession")
    async def test_health_fail(self, MockSession):
        MockSession.side_effect = Exception("err")
        a = _make_adapter()
        assert await a.health_check() is False
