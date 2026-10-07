"""Tests for symlink operations: symlink, readlink, follow through get."""

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
class TestSymlink:
    async def test_create_symlink(self, orch):
        m = await orch.symlink("/link", "/target")
        assert m.file_type == "symlink"
        assert m.symlink_target == "/target"
        assert m.mode == 0o120777

    async def test_readlink(self, orch):
        await orch.symlink("/link", "/real/path")
        target = await orch.readlink("/link")
        assert target == "/real/path"

    async def test_readlink_nonexistent_raises(self, orch):
        with pytest.raises(FileNotFoundError):
            await orch.readlink("/nope")

    async def test_readlink_regular_file_raises(self, orch):
        await orch.put("/file.txt", b"data", "pw")
        with pytest.raises(OSError, match="not a symlink"):
            await orch.readlink("/file.txt")


@pytest.mark.asyncio
class TestFollowSymlinks:
    async def test_get_through_symlink(self, orch):
        data = b"secret message"
        await orch.put("/real.txt", data, "pw")
        await orch.symlink("/alias.txt", "/real.txt")
        result = await orch.get("/alias.txt", "pw")
        assert result == data

    async def test_get_no_follow(self, orch):
        await orch.put("/real.txt", b"data", "pw")
        await orch.symlink("/alias.txt", "/real.txt")
        with pytest.raises(FileNotFoundError):
            await orch.get("/alias.txt", "pw", follow_symlinks=False)

    async def test_chained_symlinks(self, orch):
        data = b"through the chain"
        await orch.put("/origin.txt", data, "pw")
        await orch.symlink("/hop1", "/origin.txt")
        await orch.symlink("/hop2", "/hop1")
        result = await orch.get("/hop2", "pw")
        assert result == data

    async def test_dangling_symlink_raises(self, orch):
        await orch.symlink("/broken", "/nonexistent")
        with pytest.raises(FileNotFoundError):
            await orch.get("/broken", "pw")

    async def test_symlink_loop_raises(self, orch):
        await orch.symlink("/a", "/b")
        await orch.symlink("/b", "/a")
        with pytest.raises(OSError, match="too many levels"):
            await orch.get("/a", "pw")
