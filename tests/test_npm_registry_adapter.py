"""Tests for the npm registry adapter -- mocked, no real registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.npm_registry import NpmRegistryAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kw) -> NpmRegistryAdapter:
    defaults = dict(token="test-token", registry_url="https://registry.example.com")
    defaults.update(kw)
    return NpmRegistryAdapter(**defaults)


def _mock_response(method="get", status=200, json_data=None, text_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.closed = False
    setattr(session, method, MagicMock(return_value=cm))
    return session


@pytest.mark.asyncio
class TestNpmProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "npm-registry"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True
        assert p.rate_limit_per_min == 30

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=1000)
        assert a.profile().max_chunk_bytes == 1000


@pytest.mark.asyncio
class TestNpmWrite:
    async def test_write_chunk(self):
        a = _make_adapter()
        a._session = _mock_response("put")
        ref = await a.write_chunk(_sample_encoded())
        assert ref.adapter_name == "npm-registry"
        assert ref.location.startswith("npm://")

    async def test_write_opaque(self):
        a = _make_adapter()
        a._session = _mock_response("put")
        ref = await a.write_chunk("opaque-sealed-data")
        assert ref.adapter_name == "npm-registry"


@pytest.mark.asyncio
class TestNpmRead:
    async def test_read_chunk(self):
        enc = _sample_encoded()
        a = _make_adapter()
        a._session = _mock_response("get", json_data={"description": enc})
        ref = StoredChunkRef(adapter_name="npm-registry", location="npm://@picard/pchk-abc/1.0.0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_not_found(self):
        a = _make_adapter()
        a._session = _mock_response("get", json_data={"description": ""})
        ref = StoredChunkRef(adapter_name="npm-registry", location="npm://@picard/missing/1.0.0")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestNpmDelete:
    async def test_delete_success(self):
        a = _make_adapter()
        a._session = _mock_response("delete")
        ref = StoredChunkRef(adapter_name="npm-registry", location="npm://@picard/pchk-abc/1.0.0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_failure(self):
        a = _make_adapter()
        a._session = AsyncMock()
        a._session.closed = False
        a._session.delete = MagicMock(side_effect=Exception("network error"))
        ref = StoredChunkRef(adapter_name="npm-registry", location="npm://@picard/missing/1.0.0")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestNpmHealth:
    async def test_health_ok(self):
        a = _make_adapter()
        a._session = _mock_response("get")
        assert await a.health_check() is True

    async def test_health_fail(self):
        a = _make_adapter()
        a._session = AsyncMock()
        a._session.closed = False
        a._session.get = MagicMock(side_effect=Exception("connection refused"))
        assert await a.health_check() is False
