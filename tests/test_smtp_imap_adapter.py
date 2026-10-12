"""Tests for the SMTP/IMAP adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.smtp_imap import SmtpImapAdapter
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


def _make_adapter(**kwargs) -> SmtpImapAdapter:
    defaults = dict(
        smtp_host="smtp.example.com",
        imap_host="imap.example.com",
        username="user@example.com",
        password="secret",
    )
    defaults.update(kwargs)
    return SmtpImapAdapter(**defaults)


def _mock_imap():
    imap = MagicMock()
    imap.login.return_value = ("OK", [b"Logged in"])
    imap.select.return_value = ("OK", [b"1"])
    imap.create.return_value = ("OK", [b"Created"])
    return imap


@pytest.mark.asyncio
class TestSmtpImapAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "smtp-imap"
        assert p.max_chunk_bytes == 500_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        smtp = MagicMock()
        a._smtp = smtp
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "smtp-imap"
        assert len(ref.location) == 16
        smtp.sendmail.assert_called_once()
        args = smtp.sendmail.call_args[0]
        assert args[0] == "user@example.com"
        assert args[1] == ["user@example.com"]
        assert "PCHK m1" in args[2]
        assert "X-PCHK-Id" in args[2]

    async def test_read_chunk(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        enc = _sample_encoded()

        raw_email = (
            f"From: user@example.com\r\n"
            f"Subject: PCHK m1 b0 c0 [abc123]\r\n"
            f"X-PCHK-Id: abc123\r\n"
            f"Content-Type: text/plain; charset=\"utf-8\"\r\n"
            f"Content-Transfer-Encoding: base64\r\n"
            f"\r\n"
        ).encode()
        import base64
        raw_email += base64.b64encode(enc.encode())

        imap.search.return_value = ("OK", [b"1"])
        imap.fetch.return_value = ("OK", [(b"1 (RFC822 {100})", raw_email)])

        ref = StoredChunkRef(adapter_name="smtp-imap", location="abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.return_value = ("OK", [b""])

        ref = StoredChunkRef(adapter_name="smtp-imap", location="nonexistent")
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.return_value = ("OK", [b"1 2"])
        imap.store.return_value = ("OK", [])
        imap.expunge.return_value = ("OK", [])

        ref = StoredChunkRef(adapter_name="smtp-imap", location="abc123")
        assert await a.delete_chunk(ref) is True
        assert imap.store.call_count == 2
        imap.expunge.assert_called_once()

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.return_value = ("OK", [b""])

        ref = StoredChunkRef(adapter_name="smtp-imap", location="gone")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="smtp-imap", location="err")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.return_value = ("OK", [b"1 2"])
        imap.fetch.side_effect = [
            ("OK", [(b"1", b"X-PCHK-Id: id1\r\n")]),
            ("OK", [(b"2", b"X-PCHK-Id: id2\r\n")]),
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        locs = {r.location for r in refs}
        assert "id1" in locs
        assert "id2" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap
        imap.search.return_value = ("OK", [b""])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        imap = _mock_imap()
        a._imap = imap

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._imap = None
        with patch("picard.adapters.smtp_imap.imaplib", create=True) as mock_imap:
            mock_imap.IMAP4_SSL.side_effect = Exception("connection refused")
            # Force reimport to fail
            a._imap = None

            # Directly test health via exception in _ensure_imap
            import imaplib
            with patch.object(imaplib, "IMAP4_SSL", side_effect=Exception("refused")):
                assert await a.health_check() is False

    async def test_custom_folder(self):
        a = _make_adapter(folder="CHUNKS")
        assert a._folder == "CHUNKS"

    async def test_no_ssl(self):
        a = _make_adapter(use_ssl=False)
        assert a._use_ssl is False
