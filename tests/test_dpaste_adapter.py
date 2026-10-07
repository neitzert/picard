"""Tests for the dpaste adapter -- mocked, no real dpaste calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.dpaste_adapter import DpasteAdapter
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


def _make_adapter(**kwargs) -> DpasteAdapter:
    defaults = dict(api_url="https://dpaste.org/api/", expiry_days=365)
    defaults.update(kwargs)
    return DpasteAdapter(**defaults)


def _mock_response(status=200, json_data=None, text_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    return resp


@pytest.mark.asyncio
class TestDpasteAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "dpaste"
        assert p.max_chunk_bytes == 250_000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        post_resp = _mock_response(text_data="https://dpaste.org/abc123\n")

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=post_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "dpaste"
        assert ref.location == "dpaste://abc123"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        get_resp = _mock_response(text_data=enc)

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            ref = StoredChunkRef(adapter_name="dpaste", location="dpaste://abc123")
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="dpaste", location="dpaste://abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_always_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        get_resp = _mock_response(status=200)

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(side_effect=Exception("connection refused"))

            assert await a.health_check() is False

    async def test_write_chunk_slug_extraction(self):
        a = _make_adapter()

        post_resp = _mock_response(text_data="https://dpaste.org/xyz789/\n")

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=post_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            ref = await a.write_chunk(_sample_encoded())

        assert ref.location == "dpaste://xyz789"

    async def test_profile_custom_max_bytes(self):
        a = _make_adapter(max_chunk_bytes=100_000)
        assert a.profile().max_chunk_bytes == 100_000

    async def test_read_chunk_extracts_slug(self):
        a = _make_adapter()
        enc = _sample_encoded()

        get_resp = _mock_response(text_data=enc)

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            ref = StoredChunkRef(adapter_name="dpaste", location="dpaste://myslug")
            await a.read_chunk(ref)

        call_args = session.get.call_args
        assert "myslug.txt" in str(call_args)
