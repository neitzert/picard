"""Tests for versioning: put twice, list_versions, get_version, rollback."""

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator


@pytest.fixture
def orch():
    adapter = MemoryAdapter(max_chunk_bytes=256)
    index = Index()
    return Orchestrator([adapter], index, block_size=128)


@pytest.mark.asyncio
class TestVersioning:
    async def test_put_increments_version(self, orch):
        m1 = await orch.put("/doc.txt", b"v1", "pw")
        m2 = await orch.put("/doc.txt", b"v2", "pw")
        assert m1.version == 1
        assert m2.version == 2

    async def test_get_returns_latest(self, orch):
        await orch.put("/doc.txt", b"old", "pw")
        await orch.put("/doc.txt", b"new", "pw")
        result = await orch.get("/doc.txt", "pw")
        assert result == b"new"

    async def test_list_versions(self, orch):
        await orch.put("/doc.txt", b"v1", "pw")
        await orch.put("/doc.txt", b"v2", "pw")
        await orch.put("/doc.txt", b"v3", "pw")
        versions = await orch.list_versions("/doc.txt")
        assert len(versions) == 3
        assert versions[0]["version"] == 1
        assert versions[2]["version"] == 3

    async def test_get_version_specific(self, orch):
        await orch.put("/doc.txt", b"first", "pw")
        await orch.put("/doc.txt", b"second", "pw")
        result = await orch.get_version("/doc.txt", 1, "pw")
        assert result == b"first"

    async def test_get_version_nonexistent_raises(self, orch):
        await orch.put("/doc.txt", b"data", "pw")
        with pytest.raises(FileNotFoundError):
            await orch.get_version("/doc.txt", 99, "pw")

    async def test_rollback(self, orch):
        await orch.put("/doc.txt", b"original", "pw")
        await orch.put("/doc.txt", b"changed", "pw")
        rolled = await orch.rollback("/doc.txt", 1)
        assert rolled.version == 3
        result = await orch.get("/doc.txt", "pw")
        assert result == b"original"

    async def test_rollback_nonexistent_raises(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.rollback("/nope.txt", 1)

    async def test_versions_independent_paths(self, orch):
        await orch.put("/a.txt", b"a1", "pw")
        await orch.put("/b.txt", b"b1", "pw")
        await orch.put("/a.txt", b"a2", "pw")
        a_versions = await orch.list_versions("/a.txt")
        b_versions = await orch.list_versions("/b.txt")
        assert len(a_versions) == 2
        assert len(b_versions) == 1
