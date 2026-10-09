"""Tests for the PyPI adapter -- mocked, no real PyPI."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.pypi_adapter import PypiAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kw) -> PypiAdapter:
    defaults = dict(username="__token__", password="pypi-test-token", index_url="https://pypi.example.com")
    defaults.update(kw)
    return PypiAdapter(**defaults)


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
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.mark.asyncio
class TestPypiProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "pypi"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=2000)
        assert a.profile().max_chunk_bytes == 2000


@pytest.mark.asyncio
class TestPypiWrite:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    @patch("aiohttp.FormData")
    async def test_write_chunk(self, MockForm, MockAuth, MockSession):
        session_cm, session = _mock_session("post")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk(_sample_encoded())
        assert ref.adapter_name == "pypi"
        assert ref.location.startswith("pypi://")

    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    @patch("aiohttp.FormData")
    async def test_write_opaque(self, MockForm, MockAuth, MockSession):
        session_cm, session = _mock_session("post")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk("opaque-data")
        assert ref.adapter_name == "pypi"


@pytest.mark.asyncio
class TestPypiRead:
    async def test_read_chunk(self):
        enc = _sample_encoded()
        resp = AsyncMock()
        resp.status = 200
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={"info": {"description": enc}})
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        session = AsyncMock()
        session.closed = False
        session.get = MagicMock(return_value=cm)
        a = _make_adapter()
        a._session = session
        ref = StoredChunkRef(adapter_name="pypi", location="pypi://picard-chunk-abc/0.0.1")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_not_found(self):
        resp = AsyncMock()
        resp.status = 200
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={"info": {"description": ""}})
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        session = AsyncMock()
        session.closed = False
        session.get = MagicMock(return_value=cm)
        a = _make_adapter()
        a._session = session
        ref = StoredChunkRef(adapter_name="pypi", location="pypi://missing/0.0.1")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestPypiDelete:
    @patch("aiohttp.ClientSession")
    @patch("aiohttp.BasicAuth")
    async def test_delete_success(self, MockAuth, MockSession):
        session_cm, _ = _mock_session("delete")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="pypi", location="pypi://picard-chunk-abc/0.0.1")
        assert await a.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestPypiHealth:
    async def test_health_ok(self):
        resp = AsyncMock()
        resp.status = 200
        resp.raise_for_status = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        session = AsyncMock()
        session.closed = False
        session.get = MagicMock(return_value=cm)
        a = _make_adapter()
        a._session = session
        assert await a.health_check() is True

    async def test_health_fail(self):
        session = AsyncMock()
        session.closed = False
        session.get = MagicMock(side_effect=Exception("err"))
        a = _make_adapter()
        a._session = session
        assert await a.health_check() is False
