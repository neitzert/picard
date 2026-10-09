"""Tests for the SQLite index."""

import threading

from picard.index import Index
from picard.manifest import BlockRecord, ChunkRecord, ChunkStatus, Manifest


def _sample_manifest(path: str = "/hello.txt") -> Manifest:
    return Manifest(
        path=path,
        original_size=100,
        block_size=64,
        total_blocks=1,
        created_at="2025-01-01T00:00:00Z",
        blocks=[
            BlockRecord(
                block_index=0,
                digest="abc123",
                size_bytes=120,
                chunks=[
                    ChunkRecord(
                        chunk_index=0,
                        adapter_name="memory",
                        location="mem://test/0/0",
                        status=ChunkStatus.STORED,
                        size_bytes=60,
                    ),
                    ChunkRecord(
                        chunk_index=1,
                        adapter_name="memory",
                        location="mem://test/0/1",
                        status=ChunkStatus.STORED,
                        size_bytes=60,
                    ),
                ],
            )
        ],
    )


class TestIndex:
    def test_save_and_load_by_id(self):
        idx = Index()
        m = _sample_manifest()
        idx.save_manifest(m)

        loaded = idx.get_manifest(m.manifest_id)
        assert loaded is not None
        assert loaded.path == m.path
        assert loaded.total_blocks == 1
        assert len(loaded.blocks) == 1
        assert len(loaded.blocks[0].chunks) == 2

    def test_load_by_path(self):
        idx = Index()
        m = _sample_manifest("/docs/readme.md")
        idx.save_manifest(m)

        loaded = idx.get_manifest_by_path("/docs/readme.md")
        assert loaded is not None
        assert loaded.manifest_id == m.manifest_id

    def test_list_paths(self):
        idx = Index()
        idx.save_manifest(_sample_manifest("/a.txt"))
        idx.save_manifest(_sample_manifest("/b.txt"))
        paths = idx.list_paths()
        assert "/a.txt" in paths
        assert "/b.txt" in paths

    def test_delete(self):
        idx = Index()
        m = _sample_manifest()
        idx.save_manifest(m)
        assert idx.delete_manifest(m.manifest_id)
        assert idx.get_manifest(m.manifest_id) is None

    def test_delete_nonexistent(self):
        idx = Index()
        assert not idx.delete_manifest("nope")

    def test_update_chunk_status(self):
        idx = Index()
        m = _sample_manifest()
        idx.save_manifest(m)

        idx.update_chunk_status(
            m.manifest_id, 0, 0, ChunkStatus.VERIFIED, location="new://loc"
        )
        loaded = idx.get_manifest(m.manifest_id)
        assert loaded is not None
        chunk = loaded.blocks[0].chunks[0]
        assert chunk.status == ChunkStatus.VERIFIED
        assert chunk.location == "new://loc"

    def test_concurrent_version_increment(self):
        idx = Index()
        versions: list[int] = []
        errors: list[Exception] = []

        def _get_version():
            try:
                v = idx.get_next_version("/race.txt")
                versions.append(v)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_get_version) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(versions) == 10

    def test_cross_thread_access(self):
        idx = Index()
        m = _sample_manifest("/thread.txt")
        idx.save_manifest(m)
        result = [None]

        def _read():
            result[0] = idx.get_manifest_by_path("/thread.txt")

        t = threading.Thread(target=_read)
        t.start()
        t.join()
        assert result[0] is not None
        assert result[0].path == "/thread.txt"


def _manifest_with_parent(
    path: str,
    parent: str,
    *,
    file_type: str = "regular",
    size: int = 100,
    version: int = 1,
    adapter: str = "memory",
    chunk_size: int = 60,
    mode: int = 0o100644,
    mtime: str = "2025-06-01T00:00:00Z",
    manifest_id: str | None = None,
) -> Manifest:
    m = Manifest(
        path=path,
        original_size=size,
        block_size=64,
        total_blocks=1,
        created_at="2025-01-01T00:00:00Z",
        file_type=file_type,
        version=version,
        parent_path=parent,
        mode=mode,
        mtime=mtime,
    )
    if manifest_id:
        m.manifest_id = manifest_id
    if file_type != "directory":
        m.blocks = [
            BlockRecord(
                block_index=0,
                digest="d" + m.manifest_id[:7],
                size_bytes=chunk_size * 2,
                chunks=[
                    ChunkRecord(
                        chunk_index=0,
                        adapter_name=adapter,
                        location=f"{adapter}://{path}/0/0",
                        status=ChunkStatus.STORED,
                        size_bytes=chunk_size,
                    ),
                    ChunkRecord(
                        chunk_index=1,
                        adapter_name=adapter,
                        location=f"{adapter}://{path}/0/1",
                        status=ChunkStatus.STORED,
                        size_bytes=chunk_size,
                    ),
                ],
            )
        ]
    return m


def _build_tree(idx: Index) -> None:
    """Populate index with a small directory tree for testing.

    /
    ├── docs/           (directory)
    │   ├── readme.md   (regular, 200B, adapter=s3)
    │   └── notes.txt   (regular, 50B, adapter=memory)
    ├── src/            (directory)
    │   └── main.py     (regular, 300B, adapter=redis)
    └── root.txt        (regular, 100B, adapter=memory)
    """
    idx.save_manifest(_manifest_with_parent("/docs", "/", file_type="directory", size=0))
    idx.save_manifest(_manifest_with_parent("/src", "/", file_type="directory", size=0))
    idx.save_manifest(
        _manifest_with_parent("/docs/readme.md", "/docs", size=200, adapter="s3", chunk_size=100)
    )
    idx.save_manifest(
        _manifest_with_parent("/docs/notes.txt", "/docs", size=50, adapter="memory", chunk_size=25)
    )
    idx.save_manifest(
        _manifest_with_parent("/src/main.py", "/src", size=300, adapter="redis", chunk_size=150)
    )
    idx.save_manifest(
        _manifest_with_parent("/root.txt", "/", size=100, adapter="memory", chunk_size=50)
    )


class TestListFileSummaries:
    def test_empty_index(self):
        idx = Index()
        assert idx.list_file_summaries() == []

    def test_returns_all_files(self):
        idx = Index()
        _build_tree(idx)
        summaries = idx.list_file_summaries()
        paths = [s["path"] for s in summaries]
        assert "/docs" in paths
        assert "/docs/readme.md" in paths
        assert "/root.txt" in paths

    def test_summary_fields(self):
        idx = Index()
        _build_tree(idx)
        summaries = {s["path"]: s for s in idx.list_file_summaries()}
        readme = summaries["/docs/readme.md"]
        assert readme["original_size"] == 200
        assert readme["total_blocks"] == 1
        assert readme["file_type"] == "regular"
        assert readme["total_chunks"] == 2
        assert readme["adapters"] == ["s3"]
        assert "manifest_id" in readme
        assert "created_at" in readme

    def test_latest_version_only(self):
        idx = Index()
        idx.save_manifest(
            _manifest_with_parent("/file.txt", "/", size=100, version=1, adapter="s3")
        )
        idx.save_manifest(
            _manifest_with_parent("/file.txt", "/", size=200, version=2, adapter="redis")
        )
        summaries = idx.list_file_summaries()
        file_entries = [s for s in summaries if s["path"] == "/file.txt"]
        assert len(file_entries) == 1
        assert file_entries[0]["original_size"] == 200
        assert file_entries[0]["adapters"] == ["redis"]

    def test_multi_adapter_chunks(self):
        idx = Index()
        m = Manifest(
            path="/multi.txt",
            original_size=100,
            block_size=64,
            total_blocks=1,
            created_at="2025-01-01T00:00:00Z",
            parent_path="/",
            blocks=[
                BlockRecord(
                    block_index=0,
                    digest="abc",
                    size_bytes=120,
                    chunks=[
                        ChunkRecord(0, "s3", "s3://a", ChunkStatus.STORED, 60),
                        ChunkRecord(1, "redis", "redis://b", ChunkStatus.STORED, 60),
                    ],
                )
            ],
        )
        idx.save_manifest(m)
        summaries = idx.list_file_summaries()
        assert len(summaries) == 1
        assert sorted(summaries[0]["adapters"]) == ["redis", "s3"]
        assert summaries[0]["total_chunks"] == 2

    def test_directory_has_zero_chunks(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/mydir", "/", file_type="directory", size=0))
        summaries = idx.list_file_summaries()
        d = [s for s in summaries if s["path"] == "/mydir"][0]
        assert d["total_chunks"] == 0
        assert d["adapters"] == []
        assert d["file_type"] == "directory"


class TestListChildrenWithMetadata:
    def test_empty_parent(self):
        idx = Index()
        assert idx.list_children_with_metadata("/nonexistent") == []

    def test_returns_direct_children(self):
        idx = Index()
        _build_tree(idx)
        children = idx.list_children_with_metadata("/")
        paths = [c["path"] for c in children]
        assert "/docs" in paths
        assert "/src" in paths
        assert "/root.txt" in paths
        assert "/docs/readme.md" not in paths

    def test_metadata_fields(self):
        idx = Index()
        _build_tree(idx)
        children = {c["path"]: c for c in idx.list_children_with_metadata("/")}
        docs = children["/docs"]
        assert docs["type"] == "directory"
        assert docs["name"] == "docs"
        assert docs["size"] == 0
        assert docs["version"] == 1
        assert "mtime" in docs
        assert "mode" in docs

    def test_nested_children(self):
        idx = Index()
        _build_tree(idx)
        children = idx.list_children_with_metadata("/docs")
        paths = [c["path"] for c in children]
        assert "/docs/readme.md" in paths
        assert "/docs/notes.txt" in paths
        assert len(paths) == 2

    def test_latest_version_only(self):
        idx = Index()
        idx.save_manifest(
            _manifest_with_parent("/file.txt", "/", size=100, version=1, mtime="old")
        )
        idx.save_manifest(
            _manifest_with_parent("/file.txt", "/", size=200, version=2, mtime="new")
        )
        children = idx.list_children_with_metadata("/")
        file_entries = [c for c in children if c["path"] == "/file.txt"]
        assert len(file_entries) == 1
        assert file_entries[0]["size"] == 200
        assert file_entries[0]["mtime"] == "new"

    def test_name_extraction(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/deep/nested/file.txt", "/deep/nested", size=10))
        children = idx.list_children_with_metadata("/deep/nested")
        assert children[0]["name"] == "file.txt"


class TestListChildrenRich:
    def test_empty(self):
        idx = Index()
        assert idx.list_children_rich("/nowhere") == []

    def test_returns_path_filetype_size(self):
        idx = Index()
        _build_tree(idx)
        children = idx.list_children_rich("/docs")
        assert len(children) == 2
        by_path = {c["path"]: c for c in children}
        assert by_path["/docs/readme.md"]["file_type"] == "regular"
        assert by_path["/docs/readme.md"]["original_size"] == 200
        assert by_path["/docs/notes.txt"]["original_size"] == 50

    def test_latest_version(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/a.txt", "/", size=10, version=1))
        idx.save_manifest(_manifest_with_parent("/a.txt", "/", size=20, version=2))
        children = idx.list_children_rich("/")
        entries = [c for c in children if c["path"] == "/a.txt"]
        assert len(entries) == 1
        assert entries[0]["original_size"] == 20


class TestTreeQuery:
    def test_empty(self):
        idx = Index()
        assert idx.tree_query("/") == []

    def test_full_tree(self):
        idx = Index()
        _build_tree(idx)
        tree = idx.tree_query("/")
        paths = [n["path"] for n in tree]
        assert "/docs" in paths
        assert "/docs/readme.md" in paths
        assert "/src" in paths
        assert "/src/main.py" in paths
        assert "/root.txt" in paths

    def test_subtree(self):
        idx = Index()
        _build_tree(idx)
        tree = idx.tree_query("/docs")
        paths = [n["path"] for n in tree]
        assert "/docs/readme.md" in paths
        assert "/docs/notes.txt" in paths
        assert "/src/main.py" not in paths
        assert "/root.txt" not in paths

    def test_depth_limit(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/a", "/", file_type="directory", size=0))
        idx.save_manifest(_manifest_with_parent("/a/b", "/a", file_type="directory", size=0))
        idx.save_manifest(_manifest_with_parent("/a/b/c", "/a/b", file_type="directory", size=0))
        idx.save_manifest(_manifest_with_parent("/a/b/c/deep.txt", "/a/b/c", size=10))
        tree_d0 = idx.tree_query("/", max_depth=0)
        paths_d0 = [n["path"] for n in tree_d0]
        assert "/a" in paths_d0
        assert "/a/b" not in paths_d0

        tree_d1 = idx.tree_query("/", max_depth=1)
        paths_d1 = [n["path"] for n in tree_d1]
        assert "/a" in paths_d1
        assert "/a/b" in paths_d1
        assert "/a/b/c" not in paths_d1

    def test_tree_fields(self):
        idx = Index()
        _build_tree(idx)
        tree = idx.tree_query("/docs")
        readme = [n for n in tree if n["path"] == "/docs/readme.md"][0]
        assert readme["file_type"] == "regular"
        assert readme["original_size"] == 200
        assert readme["parent_path"] == "/docs"
        assert readme["depth"] == 0

    def test_latest_version_only(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/f.txt", "/", size=10, version=1))
        idx.save_manifest(_manifest_with_parent("/f.txt", "/", size=20, version=2))
        tree = idx.tree_query("/")
        entries = [n for n in tree if n["path"] == "/f.txt"]
        assert len(entries) == 1
        assert entries[0]["original_size"] == 20

    def test_sorted_by_path(self):
        idx = Index()
        _build_tree(idx)
        tree = idx.tree_query("/")
        paths = [n["path"] for n in tree]
        assert paths == sorted(paths)


class TestDuStats:
    def test_empty(self):
        idx = Index()
        stats, fc = idx.du_stats()
        assert stats == {}
        assert fc == 0

    def test_single_adapter(self):
        idx = Index()
        idx.save_manifest(
            _manifest_with_parent("/a.txt", "/", size=100, adapter="memory", chunk_size=50)
        )
        stats, fc = idx.du_stats()
        assert "memory" in stats
        assert stats["memory"]["chunk_count"] == 2
        assert stats["memory"]["bytes"] == 100
        assert fc == 1

    def test_multiple_adapters(self):
        idx = Index()
        _build_tree(idx)
        stats, fc = idx.du_stats()
        assert "memory" in stats
        assert "s3" in stats
        assert "redis" in stats
        assert fc == 6

    def test_latest_version_only(self):
        idx = Index()
        idx.save_manifest(
            _manifest_with_parent("/f.txt", "/", size=100, version=1, adapter="old")
        )
        idx.save_manifest(
            _manifest_with_parent("/f.txt", "/", size=200, version=2, adapter="new")
        )
        stats, fc = idx.du_stats()
        assert "new" in stats
        assert "old" not in stats


class TestChunkStatsForManifest:
    def test_nonexistent_manifest(self):
        idx = Index()
        dist, total = idx.chunk_stats_for_manifest("nonexistent")
        assert dist == {}
        assert total == 0

    def test_single_adapter(self):
        idx = Index()
        m = _manifest_with_parent("/a.txt", "/", adapter="memory", chunk_size=50)
        idx.save_manifest(m)
        dist, total = idx.chunk_stats_for_manifest(m.manifest_id)
        assert dist == {"memory": 2}
        assert total == 100

    def test_multi_adapter(self):
        idx = Index()
        m = Manifest(
            path="/multi.txt",
            original_size=100,
            block_size=64,
            total_blocks=1,
            created_at="2025-01-01T00:00:00Z",
            parent_path="/",
            blocks=[
                BlockRecord(
                    block_index=0,
                    digest="abc",
                    size_bytes=120,
                    chunks=[
                        ChunkRecord(0, "s3", "s3://a", ChunkStatus.STORED, 40),
                        ChunkRecord(1, "redis", "redis://b", ChunkStatus.STORED, 60),
                        ChunkRecord(2, "s3", "s3://c", ChunkStatus.STORED, 20),
                    ],
                )
            ],
        )
        idx.save_manifest(m)
        dist, total = idx.chunk_stats_for_manifest(m.manifest_id)
        assert dist == {"s3": 2, "redis": 1}
        assert total == 120


class TestFlush:
    def test_flush_noop_on_memory_db(self):
        idx = Index()
        idx.save_manifest(_sample_manifest())
        idx.flush()

    def test_flush_writes_encrypted(self, tmp_path):
        db_path = tmp_path / "test.db"
        idx = Index(db_path, passphrase="test-pass")
        m = _sample_manifest("/flush.txt")
        idx.save_manifest(m)
        idx.flush()
        assert db_path.exists()
        raw = db_path.read_bytes()
        assert raw[:4] == b"PCIX"
        idx.close()
        idx2 = Index(db_path, passphrase="test-pass")
        loaded = idx2.get_manifest_by_path("/flush.txt")
        assert loaded is not None
        assert loaded.original_size == 100
        idx2.close()


class TestListVersions:
    def test_no_versions(self):
        idx = Index()
        assert idx.list_versions("/nofile") == []

    def test_multiple_versions(self):
        idx = Index()
        idx.save_manifest(_manifest_with_parent("/v.txt", "/", size=10, version=1))
        idx.save_manifest(_manifest_with_parent("/v.txt", "/", size=20, version=2))
        idx.save_manifest(_manifest_with_parent("/v.txt", "/", size=30, version=3))
        versions = idx.list_versions("/v.txt")
        assert len(versions) == 3
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert versions[2]["original_size"] == 30


class TestListChildren:
    def test_basic(self):
        idx = Index()
        _build_tree(idx)
        children = idx.list_children("/")
        assert "/docs" in children
        assert "/src" in children
        assert "/root.txt" in children
        assert "/docs/readme.md" not in children


class TestListPathsWithPrefix:
    def test_prefix_match(self):
        idx = Index()
        _build_tree(idx)
        matches = idx.list_paths_with_prefix("/docs")
        assert "/docs" in matches
        assert "/docs/readme.md" in matches
        assert "/docs/notes.txt" in matches
        assert "/src/main.py" not in matches

    def test_no_match(self):
        idx = Index()
        _build_tree(idx)
        assert idx.list_paths_with_prefix("/zzz") == []


class TestUpdateManifestMetadata:
    def test_update_mode(self):
        idx = Index()
        m = _manifest_with_parent("/f.txt", "/")
        idx.save_manifest(m)
        idx.update_manifest_metadata(m.manifest_id, mode=0o100755)
        loaded = idx.get_manifest(m.manifest_id)
        assert loaded.mode == 0o100755

    def test_update_mtime(self):
        idx = Index()
        m = _manifest_with_parent("/f.txt", "/")
        idx.save_manifest(m)
        idx.update_manifest_metadata(m.manifest_id, mtime="2026-01-01T00:00:00Z")
        loaded = idx.get_manifest(m.manifest_id)
        assert loaded.mtime == "2026-01-01T00:00:00Z"

    def test_ignores_disallowed_fields(self):
        idx = Index()
        m = _manifest_with_parent("/f.txt", "/", size=100)
        idx.save_manifest(m)
        idx.update_manifest_metadata(m.manifest_id, original_size=999, path="/hacked")
        loaded = idx.get_manifest(m.manifest_id)
        assert loaded.original_size == 100
        assert loaded.path == "/f.txt"

    def test_update_xattrs(self):
        idx = Index()
        m = _manifest_with_parent("/f.txt", "/")
        idx.save_manifest(m)
        idx.update_manifest_metadata(m.manifest_id, xattrs={"user.key": "val"})
        loaded = idx.get_manifest(m.manifest_id)
        assert loaded.xattrs == {"user.key": "val"}

    def test_noop_on_empty(self):
        idx = Index()
        m = _manifest_with_parent("/f.txt", "/")
        idx.save_manifest(m)
        idx.update_manifest_metadata(m.manifest_id)
