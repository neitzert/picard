"""Tests for the write-ahead journal."""

import sqlite3

import pytest

from picard.journal import Journal, JournalState


@pytest.fixture
def journal():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return Journal(conn)


class TestJournalLifecycle:
    def test_begin_creates_pending(self, journal):
        jid = journal.begin("put", "/test.txt", "manifest123")
        entries = journal.get_incomplete()
        assert len(entries) == 1
        assert entries[0].state == JournalState.PENDING

    def test_mark_in_progress(self, journal):
        jid = journal.begin("put", "/test.txt", "m1")
        journal.mark_in_progress(jid, {"blocks_written": [0]})
        entries = journal.get_incomplete()
        assert entries[0].state == JournalState.IN_PROGRESS
        assert entries[0].metadata == {"blocks_written": [0]}

    def test_commit_removes_from_incomplete(self, journal):
        jid = journal.begin("put", "/test.txt", "m1")
        journal.commit(jid)
        assert journal.get_incomplete() == []

    def test_rollback_removes_from_incomplete(self, journal):
        jid = journal.begin("put", "/test.txt", "m1")
        journal.rollback(jid)
        assert journal.get_incomplete() == []

    def test_update_metadata(self, journal):
        jid = journal.begin("put", "/test.txt", "m1")
        journal.mark_in_progress(jid)
        journal.update_metadata(jid, {"blocks_written": [0, 1, 2]})
        entries = journal.get_incomplete()
        assert entries[0].metadata["blocks_written"] == [0, 1, 2]

    def test_multiple_entries(self, journal):
        j1 = journal.begin("put", "/a.txt", "m1")
        j2 = journal.begin("put", "/b.txt", "m2")
        journal.commit(j1)
        entries = journal.get_incomplete()
        assert len(entries) == 1
        assert entries[0].path == "/b.txt"

    def test_cleanup_committed(self, journal):
        j1 = journal.begin("put", "/a.txt", "m1")
        j2 = journal.begin("put", "/b.txt", "m2")
        journal.commit(j1)
        journal.rollback(j2)
        count = journal.cleanup_committed()
        assert count == 2

    def test_entry_fields(self, journal):
        jid = journal.begin("rm", "/file.txt", "m99")
        entries = journal.get_incomplete()
        e = entries[0]
        assert e.journal_id == jid
        assert e.operation == "rm"
        assert e.path == "/file.txt"
        assert e.manifest_id == "m99"
        assert e.created_at != ""

    def test_cleanup_leaves_incomplete(self, journal):
        j1 = journal.begin("put", "/a.txt", "m1")
        j2 = journal.begin("put", "/b.txt", "m2")
        journal.commit(j1)
        journal.cleanup_committed()
        entries = journal.get_incomplete()
        assert len(entries) == 1
        assert entries[0].path == "/b.txt"

    def test_mark_in_progress_default_metadata(self, journal):
        jid = journal.begin("put", "/test.txt", "m1")
        journal.mark_in_progress(jid)
        entries = journal.get_incomplete()
        assert entries[0].metadata == {}
