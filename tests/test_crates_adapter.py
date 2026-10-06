"""Tests for the crates.io adapter -- mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.crates_adapter import CratesAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kw) -> CratesAdapter:
    defaults = dict(token="test-token", registry_url="https://crates.example.com")
    defaults.update(kw)
    return CratesAdapter(**defaults)


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
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.mark.asyncio
class TestCratesProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "crates-io"
        assert p.max_chunk_bytes == 32768
        assert p.persistent is True

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=500)
        assert a.profile().max_chunk_bytes == 500


@pytest.mark.asyncio
class TestCratesWrite:
    @patch("aiohttp.ClientSession")
    async def test_write_chunk(self, MockSession):
        session_cm, _ = _mock_session("put")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk(_sample_encoded())
        assert ref.adapter_name == "crates-io"
        assert ref.location.startswith("crates://")

    @patch("aiohttp.ClientSession")
    async def test_write_opaque(self, MockSession):
        session_cm, _ = _mock_session("put")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = await a.write_chunk("opaque-data")
        assert ref.adapter_name == "crates-io"


@pytest.mark.asyncio
class TestCratesRead:
    @patch("aiohttp.ClientSession")
    async def test_read_chunk(self, MockSession):
        enc = _sample_encoded()
        session_cm, _ = _mock_session("get", json_data={"crate": {"description": enc}})
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="crates-io", location="crates://picard-chunk-abc/0.1.0")
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("aiohttp.ClientSession")
    async def test_read_not_found(self, MockSession):
        session_cm, _ = _mock_session("get", json_data={"crate": {"description": ""}})
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="crates-io", location="crates://missing/0.1.0")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestCratesDelete:
    @patch("aiohttp.ClientSession")
    async def test_delete_success(self, MockSession):
        session_cm, _ = _mock_session("delete")
        MockSession.return_value = session_cm
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="crates-io", location="crates://picard-chunk-abc/0.1.0")
        assert await a.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestCratesHealth:
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
