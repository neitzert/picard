"""Tests for the GitHub Gist adapter -- mocked, no real GitHub calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.github_gist import GitHubGistAdapter
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


def _make_adapter(**kwargs) -> GitHubGistAdapter:
    defaults = dict(token="ghp_test123")
    defaults.update(kwargs)
    return GitHubGistAdapter(**defaults)


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
class TestGitHubGistAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "github-gist"
        assert p.max_chunk_bytes == 10_485_760
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_headers(self):
        a = _make_adapter()
        h = a._headers()
        assert h["Authorization"] == "token ghp_test123"
        assert "Accept" in h

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        post_resp = _mock_response(json_data={"id": "gist_abc123"})

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=post_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "github-gist"
        assert ref.location == "gist://gist_abc123"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        get_resp = _mock_response(json_data={
            "files": {"m1_0_0.pchk": {"content": enc}}
        })

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            ref = StoredChunkRef(adapter_name="github-gist", location="gist://gist_abc123")
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_success(self):
        a = _make_adapter()

        del_resp = _mock_response(status=204)

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            del_ctx = AsyncMock()
            del_ctx.__aenter__ = AsyncMock(return_value=del_resp)
            del_ctx.__aexit__ = AsyncMock(return_value=False)
            session.delete = MagicMock(return_value=del_ctx)

            ref = StoredChunkRef(adapter_name="github-gist", location="gist://gist_abc123")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            session.delete = MagicMock(side_effect=Exception("network error"))

            ref = StoredChunkRef(adapter_name="github-gist", location="gist://gist_abc123")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()

        get_resp = _mock_response(json_data=[
            {"id": "g1", "files": {"m1_0_0.pchk": {}}},
            {"id": "g2", "files": {"m1_0_1.pchk": {}}},
            {"id": "g3", "files": {"other.pchk": {}}},
        ])

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            refs = await a.list_chunks("m1")

        assert len(refs) == 2

    async def test_list_chunks_empty(self):
        a = _make_adapter()

        get_resp = _mock_response(json_data=[])

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            refs = await a.list_chunks("m1")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()

        get_resp = _mock_response(status=200)

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            session.get = MagicMock(side_effect=Exception("connection refused"))

            assert await a.health_check() is False

    async def test_write_chunk_fallback_name(self):
        a = _make_adapter()

        post_resp = _mock_response(json_data={"id": "gist_fallback"})

        with patch("aiohttp.ClientSession") as mock_cls:
            session = AsyncMock()
            mock_cls.return_value = session
            session.closed = False
            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=post_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "github-gist"
        assert ref.location == "gist://gist_fallback"
