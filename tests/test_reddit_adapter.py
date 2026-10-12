"""Tests for the Reddit adapter — mocked, no network calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.reddit import RedditAdapter
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


def _make_adapter() -> RedditAdapter:
    return RedditAdapter(
        client_id="cid",
        client_secret="csecret",
        username="testuser",
        password="testpass",
        subreddit="test",
    )


@pytest.fixture
def mock_reddit():
    reddit = MagicMock()
    return reddit


@pytest.mark.asyncio
class TestRedditAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "reddit"
        assert p.max_chunk_bytes == 39_000
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit
        submission = MagicMock()
        submission.id = "abc123"
        mock_reddit.subreddit.return_value.submit.return_value = submission

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "reddit"
        assert ref.location == "abc123"
        mock_reddit.subreddit.assert_called_with("test")
        call_kwargs = mock_reddit.subreddit.return_value.submit.call_args
        assert call_kwargs.kwargs["selftext"] == enc
        assert "[PCHK]" in call_kwargs.kwargs["title"]

    async def test_read_chunk(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit
        enc = _sample_encoded()
        mock_reddit.submission.return_value.selftext = enc

        ref = StoredChunkRef(adapter_name="reddit", location="abc123")
        result = await a.read_chunk(ref)

        mock_reddit.submission.assert_called_with(id="abc123")
        assert result == enc

    async def test_delete_chunk(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit

        ref = StoredChunkRef(adapter_name="reddit", location="abc123")
        assert await a.delete_chunk(ref) is True
        mock_reddit.submission.return_value.delete.assert_called_once()

    async def test_delete_chunk_failure(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit
        mock_reddit.submission.return_value.delete.side_effect = Exception("gone")

        ref = StoredChunkRef(adapter_name="reddit", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit

        sub1 = MagicMock()
        sub1.title = "[PCHK] m1 b0 c0"
        sub1.id = "id1"

        sub2 = MagicMock()
        sub2.title = "[PCHK] m1 b0 c1"
        sub2.id = "id2"

        sub3 = MagicMock()
        sub3.title = "[PCHK] m2 b0 c0"
        sub3.id = "id3"

        mock_reddit.redditor.return_value.submissions.new.return_value = [
            sub1, sub2, sub3,
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "reddit" for r in refs)

    async def test_health_check_ok(self, mock_reddit):
        a = _make_adapter()
        a._reddit = mock_reddit
        mock_reddit.user.me.return_value = SimpleNamespace(name="testuser")
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._reddit = MagicMock()
        a._reddit.user.me.side_effect = Exception("auth failed")
        assert await a.health_check() is False
