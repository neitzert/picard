"""Tests for GitNotesAdapter."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from picard.adapters.git_notes_adapter import GitNotesAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "PCHK:2:testmanifest:0:0:1:dGVzdGRhdGE="
CHUNK2 = "PCHK:2:testmanifest:1:1:1:dGVzdGRhdGEy"
CHUNK_OTHER = "PCHK:2:othermanifest:0:0:1:b3RoZXI="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def adapter(tmp_path):
    # Use tmp_path as a fake repo (git_notes won't actually run git in tests)
    return GitNotesAdapter(repo_path=str(tmp_path), ref="picard", max_chunk_bytes=65536)


def make_mock_git(responses: dict[str, str]):
    """Return a _git mock that returns responses keyed by first two args."""
    async def _git(self, *args, **kwargs):
        key = " ".join(args[:3])
        for k, v in responses.items():
            if key.startswith(k):
                return v
        return ""
    return _git


class TestProfile:
    def test_profile_fields(self, adapter):
        p = adapter.profile()
        assert p.name == "git-notes"
        assert p.max_chunk_bytes == 65536
        assert p.ordered is False


class TestHealthCheck:
    def test_health_check_ok(self, adapter):
        async def fake_git(self, *a, **kw):
            return "git version 2.40.0"
        with patch.object(GitNotesAdapter, "_git", fake_git):
            assert run(adapter.health_check()) is True

    def test_health_check_fail(self, adapter):
        async def fake_git(self, *a, **kw):
            raise RuntimeError("git not found")
        with patch.object(GitNotesAdapter, "_git", fake_git):
            assert run(adapter.health_check()) is False


class TestWriteRead:
    def test_write_read_roundtrip(self, adapter):
        call_count = [0]
        async def fake_git(self, *args, **kwargs):
            call_count[0] += 1
            if "hash-object" in args:
                return "abc123def456"
            if "notes" in args and "add" in args:
                return ""
            if "notes" in args and "show" in args:
                return CHUNK
            return ""
        with patch.object(GitNotesAdapter, "_git", fake_git):
            ref = run(adapter.write_chunk(CHUNK))
            assert ref.location == "git-notes://abc123def456"
            result = run(adapter.read_chunk(ref))
            assert result == CHUNK

    def test_read_from_cache(self, adapter):
        adapter._cache["deadbeef"] = (CHUNK, "testmanifest")
        ref = StoredChunkRef(adapter_name="git-notes", location="git-notes://deadbeef")
        git_called = []
        async def fake_git(self, *a, **kw):
            git_called.append(a)
            return ""
        with patch.object(GitNotesAdapter, "_git", fake_git):
            result = run(adapter.read_chunk(ref))
        assert result == CHUNK
        assert not git_called  # cache hit, no git call

    def test_read_not_found_raises(self, adapter):
        async def fake_git(self, *a, **kw):
            raise RuntimeError("not found")
        with patch.object(GitNotesAdapter, "_git", fake_git):
            ref = StoredChunkRef(adapter_name="git-notes", location="git-notes://nonexistent")
            with pytest.raises(KeyError):
                run(adapter.read_chunk(ref))

    def test_chunk_too_large_raises(self, adapter):
        adapter._max = 5
        with pytest.raises(ValueError):
            run(adapter.write_chunk(CHUNK))


class TestDelete:
    def test_delete_prevents_read(self, adapter):
        async def fake_git(self, *args, **kwargs):
            if "hash-object" in args:
                return "deadbeef1234"
            if "notes" in args and "add" in args:
                return ""
            if "notes" in args and "remove" in args:
                return ""
            raise RuntimeError("not found")
        with patch.object(GitNotesAdapter, "_git", fake_git):
            ref = run(adapter.write_chunk(CHUNK))
            assert run(adapter.delete_chunk(ref)) is True
            with pytest.raises(KeyError):
                run(adapter.read_chunk(ref))

    def test_delete_returns_false_when_not_found(self, adapter):
        async def fake_git(self, *a, **kw):
            raise RuntimeError("no note")
        with patch.object(GitNotesAdapter, "_git", fake_git):
            ref = StoredChunkRef(adapter_name="git-notes", location="git-notes://gone")
            assert run(adapter.delete_chunk(ref)) is False


class TestListChunks:
    def test_list_chunks_by_manifest(self, adapter):
        adapter._cache["aaa"] = (CHUNK, "testmanifest")
        adapter._cache["bbb"] = (CHUNK2, "testmanifest")
        adapter._cache["ccc"] = (CHUNK_OTHER, "othermanifest")

        async def fake_git(self, *args, **kwargs):
            if "list" in args:
                return "note1 aaa\nnote2 bbb\nnote3 ccc"
            return ""
        with patch.object(GitNotesAdapter, "_git", fake_git):
            refs = run(adapter.list_chunks("testmanifest"))
        locations = {r.location for r in refs}
        assert "git-notes://aaa" in locations
        assert "git-notes://bbb" in locations
        assert "git-notes://ccc" not in locations

    def test_list_chunks_empty(self, adapter):
        async def fake_git(self, *a, **kw):
            raise RuntimeError("no notes ref")
        with patch.object(GitNotesAdapter, "_git", fake_git):
            assert run(adapter.list_chunks("testmanifest")) == []
