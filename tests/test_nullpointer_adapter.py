"""Tests for the NullPointer (0x0.st) adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_session(response_mock):
    """Build an async-context-manager mock for aiohttp.ClientSession."""
    session = MagicMock()
    # session.post(...) returns an async-CM yielding response_mock
    post_cm = AsyncMock()
    post_cm.__aenter__ = AsyncMock(return_value=response_mock)
    post_cm.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = post_cm

    get_cm = AsyncMock()
    get_cm.__aenter__ = AsyncMock(return_value=response_mock)
    get_cm.__aexit__ = AsyncMock(return_value=False)
    session.get.return_value = get_cm

    head_cm = AsyncMock()
    head_cm.__aenter__ = AsyncMock(return_value=response_mock)
    head_cm.__aexit__ = AsyncMock(return_value=False)
    session.head.return_value = head_cm

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, session


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
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "0x0"
        assert ref.location.startswith("0x0://")

    async def test_write_chunk_location_format(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value="https://0x0.st/xyz.pchk\n")
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            ref = await a.write_chunk(enc)

        assert ref.location == "0x0://https://0x0.st/xyz.pchk"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = AsyncMock()
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value=enc)
        cm, session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="0x0", location="0x0://https://0x0.st/abc.pchk")

        with patch("aiohttp.ClientSession", return_value=cm):
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
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        resp = AsyncMock()
        resp.status = 503
        cm, session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=cm):
            assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        cm.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=cm):
            assert await a.health_check() is False

    async def test_capacity_default(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] is None
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
        assert cap["chunk_count"] is None
