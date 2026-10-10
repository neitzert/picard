"""Tests for manifest dataclasses and helpers."""

from picard.manifest import (
    BlockRecord,
    ChunkRecord,
    ChunkStatus,
    FileType,
    Manifest,
    block_digest,
)


class TestFileType:
    def test_values(self):
        assert FileType.REGULAR == "regular"
        assert FileType.DIRECTORY == "directory"
        assert FileType.SYMLINK == "symlink"


class TestChunkStatus:
    def test_lifecycle(self):
        assert ChunkStatus.PENDING == "pending"
        assert ChunkStatus.STORED == "stored"
        assert ChunkStatus.VERIFIED == "verified"
        assert ChunkStatus.MISSING == "missing"


class TestChunkRecord:
    def test_defaults(self):
        c = ChunkRecord(chunk_index=0, adapter_name="fs", location="fs://abc")
        assert c.status == ChunkStatus.PENDING
        assert c.size_bytes == 0
        assert c.digest == ""


class TestBlockRecord:
    def test_defaults(self):
        b = BlockRecord(block_index=0, digest="aabbcc")
        assert b.size_bytes == 0
        assert b.chunks == []


class TestManifest:
    def test_defaults(self):
        m = Manifest()
        assert m.path == ""
        assert m.version == 1
        assert m.file_type == "regular"
        assert m.nlink == 1
        assert m.blocks == []
        assert m.manifest_id  # auto-generated UUID

    def test_total_chunks_empty(self):
        m = Manifest()
        assert m.total_chunks == 0

    def test_total_chunks(self):
        c1 = ChunkRecord(chunk_index=0, adapter_name="a", location="a://1")
        c2 = ChunkRecord(chunk_index=1, adapter_name="b", location="b://2")
        b = BlockRecord(block_index=0, digest="abc", chunks=[c1, c2])
        m = Manifest(blocks=[b])
        assert m.total_chunks == 2

    def test_all_stored_empty(self):
        m = Manifest()
        assert m.all_stored is True

    def test_all_stored_yes(self):
        c1 = ChunkRecord(chunk_index=0, adapter_name="a", location="a://1",
                         status=ChunkStatus.STORED)
        c2 = ChunkRecord(chunk_index=1, adapter_name="b", location="b://2",
                         status=ChunkStatus.VERIFIED)
        b = BlockRecord(block_index=0, digest="abc", chunks=[c1, c2])
        m = Manifest(blocks=[b])
        assert m.all_stored is True

    def test_all_stored_no(self):
        c1 = ChunkRecord(chunk_index=0, adapter_name="a", location="a://1",
                         status=ChunkStatus.STORED)
        c2 = ChunkRecord(chunk_index=1, adapter_name="b", location="b://2",
                         status=ChunkStatus.PENDING)
        b = BlockRecord(block_index=0, digest="abc", chunks=[c1, c2])
        m = Manifest(blocks=[b])
        assert m.all_stored is False

    def test_all_stored_missing(self):
        c = ChunkRecord(chunk_index=0, adapter_name="a", location="a://1",
                        status=ChunkStatus.MISSING)
        b = BlockRecord(block_index=0, digest="abc", chunks=[c])
        m = Manifest(blocks=[b])
        assert m.all_stored is False

    def test_unique_ids(self):
        m1 = Manifest()
        m2 = Manifest()
        assert m1.manifest_id != m2.manifest_id


class TestBlockDigest:
    def test_deterministic(self):
        d1 = block_digest(b"hello")
        d2 = block_digest(b"hello")
        assert d1 == d2

    def test_different_for_different_input(self):
        assert block_digest(b"a") != block_digest(b"b")

    def test_hex_format(self):
        d = block_digest(b"test")
        assert len(d) == 64
        int(d, 16)  # valid hex

    def test_empty_input(self):
        d = block_digest(b"")
        assert len(d) == 64
