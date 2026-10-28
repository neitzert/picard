"""Tests for AnonPipeAdapter."""
from __future__ import annotations

import asyncio

import pytest

from picard.adapters.anon_pipe_adapter import AnonPipeAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "PCHK:2:testmanifest:0:0:1:dGVzdGRhdGE="
CHUNK2 = "PCHK:2:testmanifest:1:1:1:dGVzdGRhdGEy"
CHUNK_OTHER = "PCHK:2:othermanifest:0:0:1:b3RoZXI="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def adapter():
    return AnonPipeAdapter(max_chunk_bytes=65536)


class TestProfile:
    def test_profile_fields(self, adapter):
        p = adapter.profile()
        assert p.name == "anon-pipe"
        assert p.max_chunk_bytes == 65536
        assert p.ordered is False


class TestHealthCheck:
    def test_health_check_true(self, adapter):
        assert run(adapter.health_check()) is True


class TestWriteRead:
    def test_write_read_roundtrip(self, adapter):
        ref = run(adapter.write_chunk(CHUNK))
        assert ref.location.startswith("anon-pipe://")
        result = run(adapter.read_chunk(ref))
        assert result == CHUNK
        run(adapter.delete_chunk(ref))

    def test_multiple_reads_idempotent(self, adapter):
        ref = run(adapter.write_chunk(CHUNK))
        assert run(adapter.read_chunk(ref)) == CHUNK
        assert run(adapter.read_chunk(ref)) == CHUNK
        run(adapter.delete_chunk(ref))

    def test_two_chunks_independent(self, adapter):
        ref1 = run(adapter.write_chunk(CHUNK))
        ref2 = run(adapter.write_chunk(CHUNK2))
        assert run(adapter.read_chunk(ref1)) == CHUNK
        assert run(adapter.read_chunk(ref2)) == CHUNK2
        run(adapter.delete_chunk(ref1))
        run(adapter.delete_chunk(ref2))

    def test_read_not_found_raises(self, adapter):
        ref = StoredChunkRef(adapter_name="anon-pipe", location="anon-pipe://nonexistent")
        with pytest.raises(KeyError):
            run(adapter.read_chunk(ref))

    def test_chunk_too_large_raises(self, adapter):
        adapter._max = 5
        with pytest.raises(ValueError):
            run(adapter.write_chunk(CHUNK))


class TestDelete:
    def test_delete_prevents_read(self, adapter):
        ref = run(adapter.write_chunk(CHUNK))
        assert run(adapter.delete_chunk(ref)) is True
        with pytest.raises(KeyError):
            run(adapter.read_chunk(ref))

    def test_delete_returns_false_when_not_found(self, adapter):
        ref = StoredChunkRef(adapter_name="anon-pipe", location="anon-pipe://gone")
        assert run(adapter.delete_chunk(ref)) is False

    def test_close_releases_all_fds(self, adapter):
        ref1 = run(adapter.write_chunk(CHUNK))
        ref2 = run(adapter.write_chunk(CHUNK2))
        run(adapter.close())
        assert len(adapter._pipes) == 0


class TestListChunks:
    def test_list_chunks_by_manifest(self, adapter):
        ref1 = run(adapter.write_chunk(CHUNK))
        ref2 = run(adapter.write_chunk(CHUNK2))
        ref3 = run(adapter.write_chunk(CHUNK_OTHER))
        refs = run(adapter.list_chunks("testmanifest"))
        locations = {r.location for r in refs}
        assert ref1.location in locations
        assert ref2.location in locations
        assert ref3.location not in locations
        run(adapter.close())
