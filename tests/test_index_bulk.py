"""Tests for Index bulk query methods: list_file_summaries, list_children_with_metadata,
list_children_rich, tree_query, du_stats, chunk_stats_for_manifest."""

from __future__ import annotations

import pytest

from picard.index import Index
from picard.manifest import (
    BlockRecord,
    ChunkRecord,
    ChunkStatus,
    Manifest,
)


@pytest.fixture()
def idx(tmp_path):
    return Index(tmp_path / "index.db")


def _make_manifest(
    path: str,
    size: int = 100,
    file_type: str = "regular",
    parent: str = "/",
    version: int = 1,
    blocks: list[BlockRecord] | None = None,
) -> Manifest:
    return Manifest(
        path=path,
        original_size=size,
        block_size=256,
        total_blocks=len(blocks) if blocks else 0,
        created_at="2026-01-01T00:00:00Z",
        plaintext_hash="abc123",
        file_type=file_type,
        parent_path=parent,
        version=version,
        blocks=blocks or [],
    )


def _block_with_chunks(
    block_index: int, adapter_name: str = "mem-0", n_chunks: int = 2
) -> BlockRecord:
    br = BlockRecord(block_index=block_index, digest="d" * 64, size_bytes=256)
    for ci in range(n_chunks):
        br.chunks.append(
            ChunkRecord(
                chunk_index=ci,
                adapter_name=adapter_name,
                location=f"loc-{block_index}-{ci}",
                status=ChunkStatus.STORED,
                size_bytes=128,
                digest=f"cd{block_index}{ci}",
            )
        )
    return br


class TestListFileSummaries:
    def test_empty(self, idx):
        assert idx.list_file_summaries() == []

    def test_returns_latest_version(self, idx):
        idx.save_manifest(_make_manifest("/a.txt", size=10, version=1))
        idx.save_manifest(_make_manifest("/a.txt", size=20, version=2))

        summaries = idx.list_file_summaries()
        assert len(summaries) == 1
        assert summaries[0]["path"] == "/a.txt"
        assert summaries[0]["original_size"] == 20

    def test_multiple_files(self, idx):
        idx.save_manifest(_make_manifest("/x.txt", size=10))
        idx.save_manifest(_make_manifest("/y.txt", size=20))
        summaries = idx.list_file_summaries()
        paths = [s["path"] for s in summaries]
        assert sorted(paths) == ["/x.txt", "/y.txt"]

    def test_includes_chunk_counts(self, idx):
        b = _block_with_chunks(0, "mem-0", 3)
        idx.save_manifest(_make_manifest("/c.txt", blocks=[b]))
        summaries = idx.list_file_summaries()
        assert summaries[0]["total_chunks"] == 3
        assert "mem-0" in summaries[0]["adapters"]


class TestListChildrenWithMetadata:
    def test_empty_parent(self, idx):
        assert idx.list_children_with_metadata("/empty") == []

    def test_returns_children(self, idx):
        idx.save_manifest(_make_manifest("/docs", file_type="directory", parent="/"))
        idx.save_manifest(_make_manifest("/docs/a.txt", parent="/docs"))
        idx.save_manifest(_make_manifest("/docs/b.txt", parent="/docs"))
        children = idx.list_children_with_metadata("/docs")
        assert len(children) == 2
        names = sorted(c["name"] for c in children)
        assert names == ["a.txt", "b.txt"]

    def test_metadata_fields(self, idx):
        idx.save_manifest(_make_manifest("/f/x.txt", size=42, parent="/f"))
        children = idx.list_children_with_metadata("/f")
        assert children[0]["size"] == 42
        assert children[0]["type"] == "regular"
        assert "mtime" in children[0]
        assert "mode" in children[0]

    def test_latest_version_only(self, idx):
        idx.save_manifest(_make_manifest("/v/f.txt", size=10, parent="/v", version=1))
        idx.save_manifest(_make_manifest("/v/f.txt", size=99, parent="/v", version=2))
        children = idx.list_children_with_metadata("/v")
        assert len(children) == 1
        assert children[0]["size"] == 99


class TestListChildrenRich:
    def test_basic(self, idx):
        idx.save_manifest(_make_manifest("/r/a.txt", size=10, parent="/r"))
        children = idx.list_children_rich("/r")
        assert len(children) == 1
        assert children[0]["path"] == "/r/a.txt"
        assert children[0]["file_type"] == "regular"
        assert children[0]["original_size"] == 10


class TestTreeQuery:
    def test_empty(self, idx):
        assert idx.tree_query("/empty") == []

    def test_flat_children(self, idx):
        idx.save_manifest(_make_manifest("/t/a.txt", parent="/t"))
        idx.save_manifest(_make_manifest("/t/b.txt", parent="/t"))
        tree = idx.tree_query("/t")
        paths = sorted(n["path"] for n in tree)
        assert paths == ["/t/a.txt", "/t/b.txt"]

    def test_nested_dirs(self, idx):
        idx.save_manifest(_make_manifest("/t/sub", file_type="directory", parent="/t"))
        idx.save_manifest(_make_manifest("/t/sub/leaf.txt", parent="/t/sub"))
        tree = idx.tree_query("/t")
        paths = sorted(n["path"] for n in tree)
        assert "/t/sub" in paths
        assert "/t/sub/leaf.txt" in paths

    def test_max_depth(self, idx):
        idx.save_manifest(_make_manifest("/d/a", file_type="directory", parent="/d"))
        idx.save_manifest(_make_manifest("/d/a/b", file_type="directory", parent="/d/a"))
        idx.save_manifest(_make_manifest("/d/a/b/deep.txt", parent="/d/a/b"))
        tree_shallow = idx.tree_query("/d", max_depth=0)
        assert len(tree_shallow) == 1
        assert tree_shallow[0]["path"] == "/d/a"

        tree_deep = idx.tree_query("/d", max_depth=10)
        assert len(tree_deep) == 3


class TestDuStats:
    def test_empty(self, idx):
        stats, fc = idx.du_stats()
        assert stats == {}
        assert fc == 0

    def test_counts_chunks(self, idx):
        b1 = _block_with_chunks(0, "mem-0", 2)
        b2 = _block_with_chunks(1, "mem-1", 1)
        idx.save_manifest(_make_manifest("/du.txt", blocks=[b1, b2]))
        stats, fc = idx.du_stats()
        assert fc == 1
        assert stats["mem-0"]["chunk_count"] == 2
        assert stats["mem-1"]["chunk_count"] == 1

    def test_latest_version_only(self, idx):
        b_old = _block_with_chunks(0, "old-adapter", 5)
        b_new = _block_with_chunks(0, "new-adapter", 3)
        idx.save_manifest(_make_manifest("/v.txt", version=1, blocks=[b_old]))
        idx.save_manifest(_make_manifest("/v.txt", version=2, blocks=[b_new]))
        stats, fc = idx.du_stats()
        assert "new-adapter" in stats
        assert stats["new-adapter"]["chunk_count"] == 3


class TestChunkStatsForManifest:
    def test_empty(self, idx):
        dist, total = idx.chunk_stats_for_manifest("nonexistent")
        assert dist == {}
        assert total == 0

    def test_counts_and_bytes(self, idx):
        b = _block_with_chunks(0, "mem-0", 3)
        m = _make_manifest("/cs.txt", blocks=[b])
        idx.save_manifest(m)
        dist, total = idx.chunk_stats_for_manifest(m.manifest_id)
        assert dist["mem-0"] == 3
        assert total == 3 * 128


class TestListPathsWithPrefix:
    def test_empty(self, idx):
        assert idx.list_paths_with_prefix("/nope") == []

    def test_finds_matching(self, idx):
        idx.save_manifest(_make_manifest("/docs/a.txt"))
        idx.save_manifest(_make_manifest("/docs/b.txt"))
        idx.save_manifest(_make_manifest("/other/c.txt"))
        result = idx.list_paths_with_prefix("/docs")
        assert sorted(result) == ["/docs/a.txt", "/docs/b.txt"]
