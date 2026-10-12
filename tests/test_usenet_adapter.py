"""Tests for the Usenet/NNTP adapter -- mocked, no network calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.usenet import UsenetAdapter
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


def _make_adapter(**kwargs) -> UsenetAdapter:
    defaults = dict(host="news.example.com")
    defaults.update(kwargs)
    return UsenetAdapter(**defaults)


@pytest.mark.asyncio
class TestUsenetAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "usenet"
        assert p.max_chunk_bytes == 500_000
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "usenet"
        assert ref.location.startswith("<")
        assert ref.location.endswith("@picard>")
        nntp.post.assert_called_once()
        article = nntp.post.call_args[0][0]
        subjects = [
            l.decode() if isinstance(l, bytes) else l
            for l in article
            if (l.decode() if isinstance(l, bytes) else l).startswith("Subject:")
        ]
        assert any("PCHK m1" in s for s in subjects)

    async def test_read_chunk(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        enc = _sample_encoded()

        info = SimpleNamespace(lines=[
            b"From: picard@example.com",
            b"Subject: PCHK m1 b0 c0",
            b"Message-ID: <abc@picard>",
            b"",
            enc.encode(),
        ])
        nntp.article.return_value = ("220 article", info)

        ref = StoredChunkRef(adapter_name="usenet", location="<abc@picard>")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_multiline_body(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp

        info = SimpleNamespace(lines=[
            b"From: picard@example.com",
            b"",
            b"line1",
            b"line2",
        ])
        nntp.article.return_value = ("220 article", info)

        ref = StoredChunkRef(adapter_name="usenet", location="<abc@picard>")
        result = await a.read_chunk(ref)
        assert result == "line1\nline2"

    async def test_delete_chunk(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp

        ref = StoredChunkRef(adapter_name="usenet", location="<abc@picard>")
        result = await a.delete_chunk(ref)
        assert result is True
        assert nntp.post.call_count == 1
        article = nntp.post.call_args[0][0]
        control_lines = [
            l.decode() if isinstance(l, bytes) else l
            for l in article
            if (l.decode() if isinstance(l, bytes) else l).startswith("Control:")
        ]
        assert any("<abc@picard>" in c for c in control_lines)

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        nntp.post.side_effect = Exception("posting not allowed")

        ref = StoredChunkRef(adapter_name="usenet", location="<abc@picard>")
        result = await a.delete_chunk(ref)
        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        enc = _sample_encoded()

        nntp.xhdr.return_value = (
            "221 headers",
            [
                ("1", "PCHK m1 b0 c0"),
                ("2", "PCHK m1 b0 c1"),
                ("3", "PCHK m2 b0 c0"),
            ],
        )

        def _article(art_num):
            return ("220 ok", SimpleNamespace(lines=[
                f"Message-ID: <msg{art_num}@picard>".encode(),
                b"",
                b"body",
            ]))

        nntp.article.side_effect = _article

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "usenet" for r in refs)
        locs = [r.location for r in refs]
        assert "<msg1@picard>" in locs
        assert "<msg2@picard>" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        nntp.xhdr.return_value = ("221 headers", [])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_exception(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        nntp.xhdr.side_effect = Exception("no such group")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        nntp = MagicMock()
        a._client = nntp
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._client = None
        with patch.object(UsenetAdapter, "_ensure_client", side_effect=Exception("refused")):
            assert await a.health_check() is False

    async def test_build_article(self):
        a = _make_adapter(newsgroup="alt.test.picard")
        enc = _sample_encoded()
        msg_id = "<test123@picard>"
        article = a._build_article(enc, msg_id)
        decoded = [l.decode() for l in article]
        assert any("alt.test.picard" in l for l in decoded)
        assert any("test123@picard" in l for l in decoded)
        assert any("PCHK m1" in l for l in decoded)
        assert decoded[-1] == enc

    async def test_ssl_connection(self):
        a = _make_adapter(use_ssl=True, port=563)
        assert a._use_ssl is True
        assert a._port == 563
