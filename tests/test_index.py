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
