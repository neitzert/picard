"""Tests for the Mastodon adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.mastodon import MastodonAdapter, _strip_html
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


def _make_adapter(**kwargs) -> MastodonAdapter:
    defaults = dict(
        api_base_url="https://mastodon.social",
        access_token="tok-xxx",
    )
    defaults.update(kwargs)
    return MastodonAdapter(**defaults)


class TestStripHtml:
    def test_strips_p_tags(self):
        assert _strip_html("<p>hello world</p>") == "hello world"

    def test_strips_nested(self):
        assert _strip_html("<p><span>hi</span></p>") == "hi"

    def test_no_tags(self):
        assert _strip_html("plain text") == "plain text"


@pytest.mark.asyncio
class TestMastodonAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "mastodon"
        assert p.max_chunk_bytes == 500
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc = _sample_encoded()

        client.status_post.return_value = {"id": 12345, "url": "https://mastodon.social/@user/12345"}

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "mastodon"
        assert ref.location == "12345"
        client.status_post.assert_called_once_with(enc, visibility="direct")

    async def test_read_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc = _sample_encoded()

        client.status.return_value = {"content": f"<p>{enc}</p>"}

        ref = StoredChunkRef(adapter_name="mastodon", location="12345")
        result = await a.read_chunk(ref)

        assert result == enc
        client.status.assert_called_once_with(12345)

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        ref = StoredChunkRef(adapter_name="mastodon", location="12345")
        assert await a.delete_chunk(ref) is True
        client.status_delete.assert_called_once_with(12345)

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.status_delete.side_effect = Exception("not found")

        ref = StoredChunkRef(adapter_name="mastodon", location="12345")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        client.me.return_value = {"id": 99}
        page = [
            {"id": 1, "content": f"<p>{enc1}</p>"},
            {"id": 2, "content": f"<p>{enc2}</p>"},
            {"id": 3, "content": f"<p>{enc_other}</p>"},
        ]
        client.account_statuses.return_value = page
        client.fetch_next.return_value = None

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "mastodon" for r in refs)
        locs = {r.location for r in refs}
        assert locs == {"1", "2"}

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        client.me.return_value = {"id": 99}
        client.account_statuses.return_value = []
        client.fetch_next.return_value = None

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)

        client.me.return_value = {"id": 99}
        page1 = [{"id": 1, "content": f"<p>{enc1}</p>"}]
        page2 = [{"id": 2, "content": f"<p>{enc2}</p>"}]
        client.account_statuses.return_value = page1
        client.fetch_next.side_effect = [page2, None]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.instance.return_value = {"uri": "mastodon.social"}

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.instance.side_effect = Exception("connection refused")

        assert await a.health_check() is False
