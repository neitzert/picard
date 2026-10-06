"""Tests for the Maven repository adapter -- mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.maven_adapter import MavenAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kw) -> MavenAdapter:
    defaults = dict(repo_url="https://repo.example.com/maven", username="admin", password="secret")
    defaults.update(kw)
    return MavenAdapter(**defaults)


def _mock_session(method="get", status=200, json_data=None, read_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if read_data is not None:
        resp.read = AsyncMock(return_value=read_data)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    setattr(session, method, MagicMock(return_value=cm))
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.mark.asyncio
class TestMavenProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "maven"
        assert p.max_chunk_bytes == 1048576
        assert p.persistent is True

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=5000)
        assert a.profile().max_chunk_bytes == 5000


@pytest.mark.asyncio
class TestMavenWrite:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_write_chunk(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("put", status=201)
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk(_sample_encoded())
        assert ref.adapter_name == "maven"
        assert ref.location.startswith("maven://")
        assert "com.picard.chunks" in ref.location

    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_write_opaque(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("put", status=201)
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk("opaque-data")
        assert ref.adapter_name == "maven"


@pytest.mark.asyncio
class TestMavenRead:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_read_chunk(self, MockAuth, MockSession):
        enc = _sample_encoded()
        session_cm, _ = _mock_session("get", read_data=enc.encode())
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="maven", location="maven://com.picard.chunks:pchk-abc:1.0.0")
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_read_not_found(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("get", status=404)
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="maven", location="maven://com.picard.chunks:missing:1.0.0")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestMavenDelete:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_delete_success(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("delete", status=204)
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="maven", location="maven://com.picard.chunks:pchk-abc:1.0.0")
        assert await a.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestMavenHealth:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_health_ok(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("get")
        MockSession.return_value = session_cm
        a = _make_adapter()
        assert await a.health_check() is True

    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_health_fail(self, MockAuth, MockSession):
        MockSession.side_effect = Exception("err")
        a = _make_adapter()
        assert await a.health_check() is False
