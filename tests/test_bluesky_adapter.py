"""Tests for the Bluesky adapter — mocked, no network calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.bluesky import BlueskyAdapter
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


def _make_adapter() -> BlueskyAdapter:
    return BlueskyAdapter("test.bsky.social", "app-password-xxx", max_chunk_bytes=280)


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.login = AsyncMock()
    return client


@pytest.mark.asyncio
class TestBlueskyAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "bluesky"
        assert p.max_chunk_bytes == 280
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        uri = "at://did:plc:abc123/app.bsky.feed.post/xyz789"
        mock_client.send_post.return_value = SimpleNamespace(uri=uri, cid="cid1")

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        mock_client.send_post.assert_awaited_once_with(text=enc)
        assert ref.adapter_name == "bluesky"
        assert ref.location == uri

    async def test_read_chunk(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        enc = _sample_encoded()
        mock_client.get_post.return_value = SimpleNamespace(
            value=SimpleNamespace(text=enc)
        )

        ref = StoredChunkRef(
            adapter_name="bluesky",
            location="at://did:plc:abc123/app.bsky.feed.post/xyz789",
        )
        result = await a.read_chunk(ref)

        mock_client.get_post.assert_awaited_once_with("xyz789")
        assert result == enc

    async def test_read_chunk_bad_uri(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        ref = StoredChunkRef(adapter_name="bluesky", location="garbage")
        with pytest.raises(ValueError, match="cannot parse AT URI"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        uri = "at://did:plc:abc123/app.bsky.feed.post/xyz789"
        ref = StoredChunkRef(adapter_name="bluesky", location=uri)
        assert await a.delete_chunk(ref) is True
        mock_client.delete_post.assert_awaited_once_with(uri)

    async def test_delete_chunk_failure(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        mock_client.delete_post.side_effect = Exception("not found")
        ref = StoredChunkRef(adapter_name="bluesky", location="at://x/app.bsky.feed.post/y")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        def _make_feed_item(text: str, uri: str):
            return SimpleNamespace(
                post=SimpleNamespace(
                    record=SimpleNamespace(text=text),
                    uri=uri,
                )
            )

        mock_client.get_author_feed.return_value = SimpleNamespace(
            feed=[
                _make_feed_item(enc1, "at://d/app.bsky.feed.post/a"),
                _make_feed_item(enc2, "at://d/app.bsky.feed.post/b"),
                _make_feed_item(enc_other, "at://d/app.bsky.feed.post/c"),
            ],
            cursor=None,
        )

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "bluesky" for r in refs)

    async def test_health_check_ok(self, mock_client):
        a = _make_adapter()
        a._client = mock_client
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        with patch("picard.adapters.bluesky.BlueskyAdapter._ensure_client", side_effect=Exception("auth failed")):
            assert await a.health_check() is False
