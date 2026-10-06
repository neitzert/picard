"""Tests for the GitConfig adapter -- mocked, no real git config changes."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.gitconfig_adapter import GitConfigAdapter
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


def _make_adapter(**kwargs) -> GitConfigAdapter:
    defaults = dict(config_path="/tmp/test-gitconfig", section="picard")
    defaults.update(kwargs)
    return GitConfigAdapter(**defaults)


def _mock_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
class TestGitConfigAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "gitconfig"
        assert p.max_chunk_bytes == 8192
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "gitconfig"
        assert ref.location.startswith("gitconfig://picard.chunk-")
        assert a._cache[ref.location] == enc

    async def test_write_chunk_failure(self):
        a = _make_adapter()

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(1, stderr=b"error: could not write config"),
        ):
            with pytest.raises(RuntimeError, match="git config set failed"):
                await a.write_chunk(_sample_encoded())

    async def test_read_chunk_from_cache(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            ref = await a.write_chunk(enc)

        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_from_git(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = StoredChunkRef(adapter_name="gitconfig", location="gitconfig://picard.chunk-abc123")

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(0, stdout=enc.encode()),
        ):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="gitconfig", location="gitconfig://picard.chunk-missing")

        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_process(1),
        ):
            with pytest.raises(KeyError, match="chunk not found"):
                await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            ref = await a.write_chunk(enc)

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            result = await a.delete_chunk(ref)

        assert result is True
        assert ref.location not in a._cache

    async def test_delete_chunk_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="gitconfig", location="gitconfig://picard.chunk-nope")

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(5)):
            result = await a.delete_chunk(ref)

        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            await a.write_chunk(enc1)
            await a.write_chunk(enc2)
            await a.write_chunk(enc_other)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "gitconfig" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(1)):
            assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("git not found")):
            assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(0)):
            await a.write_chunk(enc)

        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] is not None
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None

    async def test_scope_args_with_path(self):
        a = _make_adapter(config_path="/home/user/.gitconfig")
        assert a._scope_args() == ["--file", "/home/user/.gitconfig"]

    async def test_scope_args_global(self):
        a = _make_adapter(config_path=None)
        assert a._scope_args() == ["--global"]
