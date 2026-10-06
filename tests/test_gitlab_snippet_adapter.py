"""Tests for the GitLab Snippet adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.gitlab_snippet import GitLabSnippetAdapter
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


def _make_adapter(**kwargs) -> GitLabSnippetAdapter:
    defaults = dict(private_token="test-token")
    defaults.update(kwargs)
    return GitLabSnippetAdapter(**defaults)


def _mock_response(status=200, text="", json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(*responses):
    session = AsyncMock()
    if responses:
        session.post = MagicMock(return_value=responses[0])
        session.get = MagicMock(return_value=responses[0])
        session.delete = MagicMock(return_value=responses[0])
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
class TestGitLabSnippetAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "gitlab-snippet"
        assert p.max_chunk_bytes == 10_485_760
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_profile_custom_max(self):
        a = _make_adapter(max_chunk_bytes=5_000_000)
        assert a.profile().max_chunk_bytes == 5_000_000

    async def test_headers(self):
        a = _make_adapter()
        h = a._headers()
        assert h == {"PRIVATE-TOKEN": "test-token"}

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = _mock_response(json_data={"id": 42})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "gitlab-snippet"
        assert ref.location == "glsnip://42"
        session.post.assert_called_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        resp = _mock_response(text=enc)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="gitlab-snippet", location="glsnip://99")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_success(self):
        a = _make_adapter()

        resp = _mock_response(status=204)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="gitlab-snippet", location="glsnip://42")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.delete_chunk(ref)

        assert result is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()

        resp = _mock_response(status=404)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="gitlab-snippet", location="glsnip://42")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.delete_chunk(ref)

        assert result is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()

        session = AsyncMock()
        session.delete = MagicMock(side_effect=Exception("network error"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="gitlab-snippet", location="glsnip://42")

        with patch("aiohttp.ClientSession", return_value=session):
            result = await a.delete_chunk(ref)

        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()

        resp = _mock_response(json_data=[
            {"id": 1, "title": "picard-m1_0_0.pchk"},
            {"id": 2, "title": "picard-m1_0_1.pchk"},
            {"id": 3, "title": "picard-other_0_0.pchk"},
        ])
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "gitlab-snippet" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()

        resp = _mock_response(json_data=[])
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await a.list_chunks("m1")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()

        resp = _mock_response(status=200)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        resp = _mock_response(status=401)
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()

        session = AsyncMock()
        session.get = MagicMock(side_effect=Exception("connection error"))
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            assert await a.health_check() is False

    async def test_custom_gitlab_url(self):
        a = _make_adapter(gitlab_url="https://gitlab.example.com")
        enc = _sample_encoded()

        resp = _mock_response(json_data={"id": 77})
        session = _mock_session(resp)

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await a.write_chunk(enc)

        assert ref.location == "glsnip://77"
