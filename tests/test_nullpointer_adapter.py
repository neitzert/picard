"""Tests for the NullPointer (0x0.st) adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.nullpointer import NullPointerAdapter
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


def _make_adapter(**kwargs) -> NullPointerAdapter:
    defaults: dict = dict(
        url="https://0x0.st",
    )
    defaults.update(kwargs)
    return NullPointerAdapter(**defaults)


def _mock_response(response_mock):
    session = MagicMock()
    session.closed = False
    for method in ("post", "get", "head"):
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=False)
        setattr(session, method, MagicMock(return_value=cm))
    return session


@pytest.mark.asyncio
class TestNullPointerAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "0x0"
        assert p.max_chunk_bytes == 536_870_912
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    def test_custom_url(self):
        a = _make_adapter(url="https://my0x0.example.com/")
        assert a._url == "https://my0x0.example.com"

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value="https://0x0.st/abc.pchk\n")
        a._session = _mock_response(resp)

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "0x0"
        assert ref.location.startswith("0x0://")

    async def test_write_chunk_location_format(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value="https://0x0.st/xyz.pchk\n")
        a._session = _mock_response(resp)

        ref = await a.write_chunk(enc)
        assert ref.location == "0x0://https://0x0.st/xyz.pchk"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value=enc)
        a._session = _mock_response(resp)

        ref = StoredChunkRef(adapter_name="0x0", location="0x0://https://0x0.st/abc.pchk")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="0x0", location="0x0://https://0x0.st/abc.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = AsyncMock()
        resp.status = 200
        a._session = _mock_response(resp)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        resp = AsyncMock()
        resp.status = 503
        a._session = _mock_response(resp)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        session = MagicMock()
        session.closed = False
        session.head = MagicMock(side_effect=Exception("connection refused"))
        a._session = session
        assert await a.health_check() is False

    async def test_capacity_default(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] is None
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
        assert cap["chunk_count"] is None
