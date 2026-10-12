"""Tests for directory operations: mkdir, rmdir, ls, tree."""

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
class TestMkdir:
    async def test_mkdir_creates_directory(self, orch):
        m = await orch.mkdir("/docs")
        assert m.file_type == "directory"
        assert m.mode == 0o040755
        assert m.original_size == 0
        assert m.total_blocks == 0

    async def test_mkdir_custom_mode(self, orch):
        m = await orch.mkdir("/private", mode=0o040700)
        assert m.mode == 0o040700

    async def test_mkdir_sets_parent_path(self, orch):
        await orch.mkdir("/top")
        m = await orch.mkdir("/top/sub")
        assert m.parent_path == "/top"

    async def test_mkdir_parents_creates_ancestors(self, orch):
        await orch.mkdir("/a/b/c", parents=True)
        paths = await orch.ls()
        assert "/a" in paths
        assert "/a/b" in paths
        assert "/a/b/c" in paths

    async def test_mkdir_parents_false_no_ancestors(self, orch):
        await orch.mkdir("/lonely")
        paths = await orch.ls()
        assert "/lonely" in paths


@pytest.mark.asyncio
class TestRmdir:
    async def test_rmdir_empty(self, orch):
        await orch.mkdir("/empty_dir")
        assert await orch.rmdir("/empty_dir")
        assert "/empty_dir" not in await orch.ls()

    async def test_rmdir_nonexistent(self, orch):
        assert not await orch.rmdir("/nope")

    async def test_rmdir_non_empty_raises(self, orch):
        await orch.mkdir("/parent")
        await orch.mkdir("/parent/child")
        with pytest.raises(OSError, match="not empty"):
            await orch.rmdir("/parent")

    async def test_rmdir_file_raises(self, orch):
        await orch.put("/file.txt", b"data", "pw")
        with pytest.raises(OSError, match="not a directory"):
            await orch.rmdir("/file.txt")


@pytest.mark.asyncio
class TestLsDirectory:
    async def test_ls_no_args_backward_compat(self, orch):
        await orch.put("/a.txt", b"a", "pw")
        paths = await orch.ls()
        assert "/a.txt" in paths

    async def test_ls_with_path_lists_children(self, orch):
        await orch.mkdir("/dir")
        await orch.put("/dir/one.txt", b"1", "pw")
        await orch.put("/dir/two.txt", b"2", "pw")
        children = await orch.ls("/dir")
        assert "/dir/one.txt" in children
        assert "/dir/two.txt" in children

    async def test_ls_empty_dir(self, orch):
        await orch.mkdir("/empty")
        assert await orch.ls("/empty") == []


@pytest.mark.asyncio
class TestTree:
    async def test_tree_empty(self, orch):
        result = await orch.tree("/")
        assert result["path"] == "/"
        assert result["children"] == []

    async def test_tree_nested(self, orch):
        await orch.mkdir("/root", parents=False)
        # Set root's parent_path to "/" so it shows as child of "/"
        await orch.mkdir("/root/sub", parents=False)
        await orch.put("/root/sub/file.txt", b"x", "pw")

        result = await orch.tree("/")
        root_children = result["children"]
        dir_names = [c["path"] for c in root_children]
        assert "/root" in dir_names

    async def test_tree_files_as_leaves(self, orch):
        await orch.mkdir("/flat")
        await orch.put("/flat/a.txt", b"a", "pw")
        result = await orch.tree("/flat")
        assert any(c["path"] == "/flat/a.txt" for c in result["children"])


@pytest.mark.asyncio
class TestEnsureParents:
    async def test_put_computes_parent_path(self, orch):
        m = await orch.put("/docs/readme.md", b"hello", "pw")
        assert m.parent_path == "/docs"

    async def test_root_file_parent_is_slash(self, orch):
        m = await orch.put("/top.txt", b"data", "pw")
        assert m.parent_path == "/"
