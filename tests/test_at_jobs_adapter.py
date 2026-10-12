"""Tests for the at(1) jobs adapter -- mocked, no real at daemon needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.at_jobs_adapter import AtJobsAdapter
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


def _make_adapter(**kwargs) -> AtJobsAdapter:
    defaults = dict(max_chunk_bytes=4096)
    defaults.update(kwargs)
    return AtJobsAdapter(**defaults)


def _mock_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
class TestAtJobsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "at-jobs"
        assert p.max_chunk_bytes == 4096
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_write_chunk(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        mock_exec.return_value = _mock_process(stderr=b"job 42 at Thu Oct  6 12:00:00 2026")

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "at-jobs"
        assert ref.location == "at://42"
        assert a._cache[ref.location] == enc

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_write_chunk_no_job_number(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        mock_exec.return_value = _mock_process(stderr=b"warning: commands will be executed using /bin/sh")

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "at-jobs"
        assert ref.location.startswith("at://")
        assert a._cache[ref.location] == enc

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_read_chunk_from_cache(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        mock_exec.return_value = _mock_process(stderr=b"job 7 at Thu Oct  6 12:00:00 2026")

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_read_chunk_from_at(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        script_body = f"#!/bin/sh\n# PICARD:abc123:{enc}\ntrue\n"
        mock_exec.return_value = _mock_process(stdout=script_body.encode())

        ref = StoredChunkRef(adapter_name="at-jobs", location="at://99")
        result = await a.read_chunk(ref)
        assert result == enc

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_read_chunk_not_found(self, mock_exec):
        a = _make_adapter()
        mock_exec.return_value = _mock_process(stdout=b"#!/bin/sh\ntrue\n")

        ref = StoredChunkRef(adapter_name="at-jobs", location="at://99")
        with pytest.raises(KeyError, match="chunk not found"):
            await a.read_chunk(ref)

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_delete_chunk(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        mock_exec.return_value = _mock_process(stderr=b"job 10 at Thu Oct  6 12:00:00 2026")

        ref = await a.write_chunk(enc)

        mock_exec.return_value = _mock_process()
        result = await a.delete_chunk(ref)
        assert result is True
        assert ref.location not in a._cache

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_delete_chunk_missing(self, mock_exec):
        a = _make_adapter()
        mock_exec.return_value = _mock_process()

        ref = StoredChunkRef(adapter_name="at-jobs", location="at://999")
        result = await a.delete_chunk(ref)
        assert result is False

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_list_chunks(self, mock_exec):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        mock_exec.return_value = _mock_process(stderr=b"job 1 at x")
        await a.write_chunk(enc1)
        mock_exec.return_value = _mock_process(stderr=b"job 2 at x")
        await a.write_chunk(enc2)
        mock_exec.return_value = _mock_process(stderr=b"job 3 at x")
        await a.write_chunk(enc_other)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "at-jobs" for r in refs)

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_health_check_ok(self, mock_exec):
        a = _make_adapter()
        mock_exec.return_value = _mock_process(returncode=0)
        assert await a.health_check() is True

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_health_check_fail(self, mock_exec):
        a = _make_adapter()
        mock_exec.side_effect = FileNotFoundError("atq not found")
        assert await a.health_check() is False

    @patch("picard.adapters.at_jobs_adapter.asyncio.create_subprocess_exec")
    async def test_capacity(self, mock_exec):
        a = _make_adapter()
        enc = _sample_encoded()
        mock_exec.return_value = _mock_process(stderr=b"job 5 at x")
        await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == len(enc.encode())
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
