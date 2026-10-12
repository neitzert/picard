"""Tests for journal integration with the orchestrator."""

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.journal import Journal
from picard.orchestrator import Orchestrator


@pytest.fixture
def setup():
    adapter = MemoryAdapter()
    index = Index(":memory:")
    journal = Journal(index.connection)
    orch = Orchestrator([adapter], index, journal=journal)
    return orch, index, journal, adapter


class TestJournalIntegration:
    @pytest.mark.asyncio
    async def test_put_creates_committed_journal(self, setup):
        orch, index, journal, _ = setup
        await orch.put("/test.txt", b"hello world", "pass123")
        assert journal.get_incomplete() == []

    @pytest.mark.asyncio
    async def test_put_journal_lifecycle(self, setup):
        orch, index, journal, _ = setup
        await orch.put("/test.txt", b"hello", "pass123")
        cleaned = journal.cleanup_committed()
        assert cleaned >= 1

    @pytest.mark.asyncio
    async def test_recover_no_incomplete(self, setup):
        orch, index, journal, _ = setup
        result = await orch.recover()
        assert result["recovered"] == 0
        assert result["cleaned_chunks"] == 0

    @pytest.mark.asyncio
    async def test_recover_cleans_incomplete(self, setup):
        orch, index, journal, adapter = setup
        mid = "deadbeef1234"
        jid = journal.begin("put", "/crash.txt", mid)
        journal.mark_in_progress(jid, {"blocks_written": [0]})
        result = await orch.recover()
        assert result["recovered"] == 1
        assert journal.get_incomplete() == []

    @pytest.mark.asyncio
    async def test_put_without_journal(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index)
        m = await orch.put("/test.txt", b"data", "pass")
        data = await orch.get("/test.txt", "pass")
        assert data == b"data"

    @pytest.mark.asyncio
    async def test_journal_rollback_on_failure(self, setup):
        orch, index, journal, adapter = setup
        original_write = adapter.write_chunk
        call_count = 0

        async def failing_write(data):
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                raise IOError("simulated failure")
            return await original_write(data)

        adapter.write_chunk = failing_write

        with pytest.raises(IOError):
            await orch.put("/fail.txt", b"x" * 200000, "pass")

        assert journal.get_incomplete() == []

    @pytest.mark.asyncio
    async def test_get_still_works_with_journal(self, setup):
        orch, index, journal, _ = setup
        await orch.put("/test.txt", b"round trip data", "pass123")
        data = await orch.get("/test.txt", "pass123")
        assert data == b"round trip data"

    @pytest.mark.asyncio
    async def test_multiple_puts_all_journaled(self, setup):
        orch, index, journal, _ = setup
        await orch.put("/a.txt", b"aaa", "pass")
        await orch.put("/b.txt", b"bbb", "pass")
        await orch.put("/c.txt", b"ccc", "pass")
        assert journal.get_incomplete() == []
        assert journal.cleanup_committed() == 3
