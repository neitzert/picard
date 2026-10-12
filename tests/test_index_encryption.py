"""Tests for encrypted index database."""

import pytest

from picard.index import Index, _PCIX_MAGIC
from picard.manifest import Manifest


class TestIndexEncryption:
    def test_encrypted_roundtrip(self, tmp_path):
        db_path = tmp_path / "test.db"
        idx = Index(db_path, passphrase="secret123")
        m = Manifest(
            path="/hello.txt", original_size=100, block_size=64,
            total_blocks=2, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        idx.close()

        raw = db_path.read_bytes()
        assert raw[:5] == _PCIX_MAGIC

        idx2 = Index(db_path, passphrase="secret123")
        loaded = idx2.get_manifest(m.manifest_id)
        assert loaded is not None
        assert loaded.path == "/hello.txt"
        assert loaded.original_size == 100
        idx2.close()

    def test_unencrypted_still_works(self, tmp_path):
        db_path = tmp_path / "plain.db"
        idx = Index(db_path)
        m = Manifest(
            path="/plain.txt", original_size=50, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        idx.close()

        raw = db_path.read_bytes()
        assert raw[:5] != _PCIX_MAGIC
        assert raw[:6] == b"SQLite"

    def test_memory_db_ignores_passphrase(self):
        idx = Index(":memory:", passphrase="secret")
        m = Manifest(
            path="/mem.txt", original_size=10, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        assert idx.get_manifest(m.manifest_id) is not None
        idx.close()

    def test_wrong_passphrase_raises(self, tmp_path):
        db_path = tmp_path / "enc.db"
        idx = Index(db_path, passphrase="correct")
        m = Manifest(
            path="/secret.txt", original_size=10, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        idx.close()

        with pytest.raises(Exception):
            Index(db_path, passphrase="wrong")

    def test_migrate_plain_to_encrypted(self, tmp_path):
        db_path = tmp_path / "migrate.db"
        idx = Index(db_path)
        m = Manifest(
            path="/data.txt", original_size=20, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        idx.close()
        assert db_path.read_bytes()[:5] != _PCIX_MAGIC

        idx2 = Index(db_path, passphrase="migrate-key")
        loaded = idx2.get_manifest(m.manifest_id)
        assert loaded is not None
        idx2.close()
        assert db_path.read_bytes()[:5] == _PCIX_MAGIC

        idx3 = Index(db_path, passphrase="migrate-key")
        assert idx3.get_manifest(m.manifest_id) is not None
        idx3.close()

    def test_flush_saves_without_closing(self, tmp_path):
        db_path = tmp_path / "flush.db"
        idx = Index(db_path, passphrase="flush-key")
        m = Manifest(
            path="/flush.txt", original_size=10, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m)
        idx.flush()

        assert db_path.read_bytes()[:5] == _PCIX_MAGIC

        m2 = Manifest(
            path="/flush2.txt", original_size=20, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
        )
        idx.save_manifest(m2)
        idx.close()

        idx2 = Index(db_path, passphrase="flush-key")
        assert idx2.get_manifest(m.manifest_id) is not None
        assert idx2.get_manifest(m2.manifest_id) is not None
        idx2.close()

    def test_list_paths_encrypted(self, tmp_path):
        db_path = tmp_path / "methods.db"
        idx = Index(db_path, passphrase="test")
        idx.save_manifest(Manifest(
            path="/a.txt", original_size=1, block_size=64,
            total_blocks=1, created_at="2026-01-01",
        ))
        idx.save_manifest(Manifest(
            path="/b.txt", original_size=2, block_size=64,
            total_blocks=1, created_at="2026-01-01",
        ))
        paths = idx.list_paths()
        assert "/a.txt" in paths
        assert "/b.txt" in paths
        idx.close()

    def test_encrypted_preserves_blocks_and_chunks(self, tmp_path):
        from picard.manifest import BlockRecord, ChunkRecord, ChunkStatus

        db_path = tmp_path / "blocks.db"
        idx = Index(db_path, passphrase="block-test")
        m = Manifest(
            path="/chunked.bin", original_size=200, block_size=64,
            total_blocks=1, created_at="2026-01-01T00:00:00Z",
            blocks=[
                BlockRecord(
                    block_index=0, digest="abc123", size_bytes=100,
                    chunks=[
                        ChunkRecord(
                            chunk_index=0, adapter_name="memory",
                            location="loc0", status=ChunkStatus.STORED,
                            size_bytes=50, digest="d0",
                        ),
                        ChunkRecord(
                            chunk_index=1, adapter_name="memory",
                            location="loc1", status=ChunkStatus.STORED,
                            size_bytes=50, digest="d1",
                        ),
                    ],
                ),
            ],
        )
        idx.save_manifest(m)
        idx.close()

        idx2 = Index(db_path, passphrase="block-test")
        loaded = idx2.get_manifest(m.manifest_id)
        assert loaded is not None
        assert len(loaded.blocks) == 1
        assert len(loaded.blocks[0].chunks) == 2
        assert loaded.blocks[0].chunks[0].location == "loc0"
        assert loaded.blocks[0].chunks[1].location == "loc1"
        idx2.close()
