"""Tests for JournaldAdapter."""
from __future__ import annotations

import asyncio
import sys

import pytest

if sys.platform != "linux":
    pytest.skip("journald_adapter requires Linux", allow_module_level=True)

from picard.adapters.journald_adapter import JournaldAdapter
from picard.adapters.base import StoredChunkRef

CHUNK = "PCHK:2:testmanifest:0:0:1:dGVzdGRhdGE="
CHUNK2 = "PCHK:2:testmanifest:1:1:1:dGVzdGRhdGEy"
CHUNK_OTHER = "PCHK:2:othermanifest:0:0:1:b3RoZXI="


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
    a = JournaldAdapter(max_chunk_bytes=65536)
    # Patch _systemd_cat_send to be a no-op
    async def _noop(self, *a, **kw):
        pass
    a._systemd_cat_send = lambda *a, **kw: _noop(a, kw)
    return a


class TestProfile:
    def test_profile_fields(self):
        a = JournaldAdapter.__new__(JournaldAdapter)
        a._max = 65536
        p = a.profile()
        assert p.name == "journald"
        assert p.max_chunk_bytes == 65536
        assert p.ordered is False


class TestHealthCheck:
    def test_health_check_with_libsystemd(self, monkeypatch):
        import types
        fake_lib = types.SimpleNamespace(sd_journal_send=lambda *a: 0)
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", fake_lib)
        a = JournaldAdapter()
        assert run(a.health_check()) is True

    def test_health_check_with_systemd_cat(self, monkeypatch):
        import shutil
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemd-cat" if x == "systemd-cat" else None)
        a = JournaldAdapter()
        assert run(a.health_check()) is True

    def test_health_check_none_available(self, monkeypatch):
        import shutil
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
        monkeypatch.setattr(shutil, "which", lambda x: None)
        a = JournaldAdapter()
        assert run(a.health_check()) is False


class TestWriteRead:
    def test_write_read_roundtrip(self, monkeypatch):
        import types
        called = []
        def fake_send(*args):
            called.append(args)
            return 0
        fake_lib = types.SimpleNamespace(sd_journal_send=fake_send)
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", fake_lib)
        a = JournaldAdapter()
        ref = run(a.write_chunk(CHUNK))
        assert ref.location.startswith("journald://")
        result = run(a.read_chunk(ref))
        assert result == CHUNK

    def test_read_not_found_raises(self, monkeypatch):
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
        a = JournaldAdapter()

        async def fake_cat(self, *a, **kw):
            pass
        a._systemd_cat_send = lambda *a, **kw: fake_cat(a, kw)

        ref = StoredChunkRef(adapter_name="journald", location="journald://nonexistent")
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))

    def test_chunk_too_large_raises(self, monkeypatch):
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
        a = JournaldAdapter(max_chunk_bytes=5)
        with pytest.raises(ValueError):
            run(a.write_chunk(CHUNK))


class TestDelete:
    def test_delete_prevents_read(self, monkeypatch):
        import types
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd",
                            types.SimpleNamespace(sd_journal_send=lambda *a: 0))
        a = JournaldAdapter()
        ref = run(a.write_chunk(CHUNK))
        assert run(a.delete_chunk(ref)) is True
        with pytest.raises(KeyError):
            run(a.read_chunk(ref))

    def test_delete_returns_false_missing(self, monkeypatch):
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd", None)
        a = JournaldAdapter()
        ref = StoredChunkRef(adapter_name="journald", location="journald://gone")
        assert run(a.delete_chunk(ref)) is False


class TestListChunks:
    def test_list_chunks_by_manifest(self, monkeypatch):
        import types
        monkeypatch.setattr("picard.adapters.journald_adapter._libsystemd",
                            types.SimpleNamespace(sd_journal_send=lambda *a: 0))
        a = JournaldAdapter()
        ref1 = run(a.write_chunk(CHUNK))
        ref2 = run(a.write_chunk(CHUNK2))
        ref3 = run(a.write_chunk(CHUNK_OTHER))
        refs = run(a.list_chunks("testmanifest"))
        locations = {r.location for r in refs}
        assert ref1.location in locations
        assert ref2.location in locations
        assert ref3.location not in locations
