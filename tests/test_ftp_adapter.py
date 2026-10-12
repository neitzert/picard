"""Tests for the FTP adapter — mocked, no network calls."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ftp import FtpAdapter
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


def _make_adapter(**kwargs) -> FtpAdapter:
    defaults = dict(host="ftp.example.com", port=21, username="user", password="pass")
    defaults.update(kwargs)
    return FtpAdapter(**defaults)


@pytest.mark.asyncio
class TestFtpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ftp"
        assert p.max_chunk_bytes == 500_000
        assert p.persistent is True
        assert p.ordered is False

    async def test_filename(self):
        assert FtpAdapter._filename("m1", 0, 2) == "pchk_m1_b0_c2.txt"
        assert FtpAdapter._filename("abc", 3, 5) == "pchk_abc_b3_c5.txt"

    async def test_write_chunk(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ftp"
        assert ref.location == "pchk_m1_b0_c0.txt"
        ftp.storbinary.assert_called_once()
        call_args = ftp.storbinary.call_args
        assert call_args[0][0] == "STOR pchk_m1_b0_c0.txt"

    async def test_read_chunk(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        enc = _sample_encoded()

        def fake_retrbinary(cmd, callback):
            callback(enc.encode())

        ftp.retrbinary.side_effect = fake_retrbinary

        ref = StoredChunkRef(adapter_name="ftp", location="pchk_m1_b0_c0.txt")
        result = await a.read_chunk(ref)

        assert result == enc
        ftp.retrbinary.assert_called_once()

    async def test_delete_chunk(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp

        ref = StoredChunkRef(adapter_name="ftp", location="pchk_m1_b0_c0.txt")
        assert await a.delete_chunk(ref) is True
        ftp.delete.assert_called_once_with("pchk_m1_b0_c0.txt")

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        ftp.delete.side_effect = Exception("not found")

        ref = StoredChunkRef(adapter_name="ftp", location="pchk_m1_b0_c0.txt")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        ftp.nlst.return_value = [
            "pchk_m1_b0_c0.txt",
            "pchk_m1_b0_c1.txt",
            "pchk_m2_b0_c0.txt",
            "other_file.txt",
        ]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "ftp" for r in refs)
        locs = [r.location for r in refs]
        assert "pchk_m1_b0_c0.txt" in locs
        assert "pchk_m1_b0_c1.txt" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        ftp.nlst.return_value = []

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp

        assert await a.health_check() is True
        ftp.voidcmd.assert_called_once_with("NOOP")

    async def test_health_check_fail(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp
        ftp.voidcmd.side_effect = Exception("connection lost")

        assert await a.health_check() is False

    async def test_ensure_client_creates_dir(self):
        import ftplib

        a = _make_adapter()
        ftp = MagicMock()
        ftp.cwd.side_effect = [ftplib.error_perm("no such dir"), None]

        with patch("picard.adapters.ftp.ftplib.FTP", return_value=ftp):
            a._ensure_client()

        ftp.connect.assert_called_once_with("ftp.example.com", 21)
        ftp.login.assert_called_once_with("user", "pass")
        ftp.mkd.assert_called_once_with("/picard")
        assert a._ftp is ftp

    async def test_ensure_client_reuses(self):
        a = _make_adapter()
        ftp = MagicMock()
        a._ftp = ftp

        assert a._ensure_client() is ftp
