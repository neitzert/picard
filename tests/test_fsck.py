"""Tests for fsck: check, repair, error detection."""

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.fsck import fsck, repair
from picard.index import Index
from picard.journal import Journal
from picard.manifest import Manifest
from picard.orchestrator import Orchestrator


@pytest.fixture
def setup():
    adapter = MemoryAdapter(max_chunk_bytes=256)
    index = Index()
    orch = Orchestrator([adapter], index, block_size=128)
    return orch, index, adapter


@pytest.mark.asyncio
class TestFsckClean:
    async def test_empty_fs_ok(self, setup):
        _, index, adapter = setup
        report = await fsck(index, [adapter])
        assert report.ok
        assert report.total_manifests == 0

    async def test_clean_file_ok(self, setup):
        orch, index, adapter = setup
        await orch.put("/test.txt", b"hello fsck", "pw")
        report = await fsck(index, [adapter])
        assert report.ok
        assert report.total_manifests == 1
        assert report.verified_chunks > 0
        assert report.missing_chunks == 0

    async def test_multiple_files_ok(self, setup):
        orch, index, adapter = setup
        await orch.put("/a.txt", b"aaa", "pw")
        await orch.put("/b.txt", b"bbb", "pw")
        report = await fsck(index, [adapter])
        assert report.ok
        assert report.total_manifests == 2


@pytest.mark.asyncio
class TestFsckMissingChunks:
    async def test_detect_missing_chunk(self, setup):
        orch, index, adapter = setup
        await orch.put("/test.txt", b"data", "pw")
        adapter.store.clear()
        report = await fsck(index, [adapter])
        assert not report.ok
        assert report.missing_chunks > 0


@pytest.mark.asyncio
class TestFsckDirectories:
    async def test_missing_parent_detected(self, setup):
        orch, index, adapter = setup
        now = "2026-01-01T00:00:00+00:00"
        m = Manifest(
            path="/orphan/file.txt",
            original_size=0,
            block_size=128,
            total_blocks=0,
            created_at=now,
            parent_path="/orphan",
            mtime=now,
            ctime=now,
            atime=now,
        )
        index.save_manifest(m)
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.directory_errors == 1
        assert any(e["type"] == "missing_parent" for e in report.errors)

    async def test_valid_parent_ok(self, setup):
        orch, index, adapter = setup
        await orch.mkdir("/docs")
        await orch.put("/docs/readme.md", b"hello", "pw")
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.directory_errors == 0


@pytest.mark.asyncio
class TestFsckSymlinks:
    async def test_dangling_symlink_detected(self, setup):
        orch, index, adapter = setup
        await orch.symlink("/broken", "/nonexistent")
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.symlink_errors == 1
        assert any(e["type"] == "dangling_symlink" for e in report.errors)

    async def test_valid_symlink_ok(self, setup):
        orch, index, adapter = setup
        await orch.put("/real.txt", b"data", "pw")
        await orch.symlink("/link", "/real.txt")
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.symlink_errors == 0


@pytest.mark.asyncio
class TestFsckJournal:
    async def test_incomplete_journal_detected(self, setup):
        _, index, adapter = setup
        journal = Journal(index.connection)
        journal.begin("put", "/crash.txt", "abc123")
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.journal_incomplete == 1

    async def test_clean_journal_ok(self, setup):
        _, index, adapter = setup
        journal = Journal(index.connection)
        jid = journal.begin("put", "/ok.txt", "abc")
        journal.commit(jid)
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.journal_incomplete == 0


@pytest.mark.asyncio
class TestVersionGaps:
    async def test_no_gap_on_normal_overwrites(self, setup):
        orch, index, adapter = setup
        await orch.put("/versioned.txt", b"v1", "pw")
        await orch.put("/versioned.txt", b"v2", "pw")
        await orch.put("/versioned.txt", b"v3", "pw")
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.version_gaps == 0

    async def test_gap_detected_when_manifest_removed(self, setup):
        orch, index, adapter = setup
        await orch.put("/gap.txt", b"v1", "pw")
        await orch.put("/gap.txt", b"v2", "pw")
        await orch.put("/gap.txt", b"v3", "pw")
        # Delete version 2 directly from the index to simulate a gap
        index._conn.execute(
            "DELETE FROM manifests WHERE path=? AND version=2", ("/gap.txt",)
        )
        index._conn.commit()
        report = await fsck(index, [adapter], verify_digests=False)
        assert report.version_gaps == 1
        gap_errors = [e for e in report.errors if e["type"] == "version_gap"]
        assert len(gap_errors) == 1
        assert gap_errors[0]["path"] == "/gap.txt"
        assert gap_errors[0]["after_version"] == 1
        assert gap_errors[0]["before_version"] == 3
        assert gap_errors[0]["missing_count"] == 1


@pytest.mark.asyncio
class TestRepair:
    async def test_repair_incomplete_journal(self, setup):
        _, index, adapter = setup
        journal = Journal(index.connection)
        journal.begin("put", "/crash.txt", "abc123")
        report = await fsck(index, [adapter], verify_digests=False)
        result = await repair(index, [adapter], report)
        assert result["fixed"] == 1
        report2 = await fsck(index, [adapter], verify_digests=False)
        assert report2.journal_incomplete == 0
