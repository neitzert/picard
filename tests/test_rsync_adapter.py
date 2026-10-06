"""Tests for the rsync adapter -- mocked subprocess, no real rsync."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.rsync_adapter import RsyncAdapter
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


def _make_adapter(tmp_path=None, **kwargs) -> RsyncAdapter:
    defaults = dict(host="backup.host", module="chunks")
    if tmp_path:
        defaults["local_staging"] = str(tmp_path / "staging")
    defaults.update(kwargs)
    return RsyncAdapter(**defaults)


def _mock_process(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.stdout = AsyncMock()
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    proc.wait = AsyncMock()
    return proc


@pytest.mark.asyncio
class TestRsyncAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "rsync"
        assert p.max_chunk_bytes == 1_048_576
        assert p.persistent is True
        assert p.rate_limit_per_min == 60

    async def test_profile_custom(self):
        a = _make_adapter(max_chunk_bytes=4096, port=8873)
        assert a.profile().max_chunk_bytes == 4096
        assert a._port == 8873

    @patch("asyncio.create_subprocess_exec")
    async def test_write_chunk(self, mock_exec, tmp_path):
        mock_exec.return_value = _mock_process(returncode=0)

        a = _make_adapter(tmp_path=tmp_path)
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "rsync"
        assert "rsync://" in ref.location
        assert "m1" in ref.location
        assert ref.location.endswith(".pchk")

    @patch("asyncio.create_subprocess_exec")
    async def test_write_opaque_chunk(self, mock_exec, tmp_path):
        mock_exec.return_value = _mock_process(returncode=0)

        a = _make_adapter(tmp_path=tmp_path)
        ref = await a.write_chunk("opaque-sealed-hex")
        assert ref.adapter_name == "rsync"

    @patch("asyncio.create_subprocess_exec")
    async def test_write_chunk_failure(self, mock_exec, tmp_path):
        mock_exec.return_value = _mock_process(returncode=1, stderr=b"timeout")

        a = _make_adapter(tmp_path=tmp_path)
        with pytest.raises(RuntimeError, match="rsync push failed"):
            await a.write_chunk(_sample_encoded())

    @patch("asyncio.create_subprocess_exec")
    async def test_read_chunk(self, mock_exec, tmp_path):
        enc = _sample_encoded()
        staging = tmp_path / "staging" / "m1"
        staging.mkdir(parents=True)
        target = staging / "0_0.pchk"

        async def _fake_exec(*args, **kwargs):
            target.write_text(enc, encoding="utf-8")
            return _mock_process(returncode=0)

        mock_exec.side_effect = _fake_exec

        a = _make_adapter(tmp_path=tmp_path)
        ref = StoredChunkRef(
            adapter_name="rsync",
            location="rsync://backup.host/chunks/m1/0_0.pchk",
        )
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("asyncio.create_subprocess_exec")
    async def test_delete_chunk(self, mock_exec, tmp_path):
        mock_exec.return_value = _mock_process(returncode=0)

        a = _make_adapter(tmp_path=tmp_path)
        ref = StoredChunkRef(
            adapter_name="rsync",
            location="rsync://backup.host/chunks/m1/0_0.pchk",
        )
        assert await a.delete_chunk(ref) is True

    @patch("asyncio.create_subprocess_exec")
    async def test_delete_chunk_fail(self, mock_exec, tmp_path):
        mock_exec.return_value = _mock_process(returncode=1)

        a = _make_adapter(tmp_path=tmp_path)
        ref = StoredChunkRef(
            adapter_name="rsync",
            location="rsync://backup.host/chunks/m1/0_0.pchk",
        )
        assert await a.delete_chunk(ref) is False

    @patch("asyncio.create_subprocess_exec")
    async def test_list_chunks(self, mock_exec):
        listing = (
            b"-rw-r--r--  1024 2026/01/01 12:00:00 0_0.pchk\n"
            b"-rw-r--r--  1024 2026/01/01 12:00:00 0_1.pchk\n"
            b"-rw-r--r--  1024 2026/01/01 12:00:00 readme.txt\n"
        )
        mock_exec.return_value = _mock_process(returncode=0, stdout=listing)

        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "rsync" for r in refs)

    @patch("asyncio.create_subprocess_exec")
    async def test_list_chunks_empty(self, mock_exec):
        mock_exec.return_value = _mock_process(returncode=1)

        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    @patch("asyncio.create_subprocess_exec")
    async def test_health_check_ok(self, mock_exec):
        mock_exec.return_value = _mock_process(returncode=0)

        a = _make_adapter()
        assert await a.health_check() is True

    @patch("asyncio.create_subprocess_exec")
    async def test_health_check_fail(self, mock_exec):
        mock_exec.return_value = _mock_process(returncode=1)

        a = _make_adapter()
        assert await a.health_check() is False

    async def test_capacity_unknown(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] is None
        assert cap["free_bytes"] is None
