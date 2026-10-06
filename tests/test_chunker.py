"""Tests for the chunker module."""

import pytest

from picard.chunker import Chunk, chunkify, reassemble


class TestChunkEncodeDecode:
    def test_round_trip(self):
        c = Chunk(
            version=1,
            manifest_id="abc123",
            block_index=0,
            chunk_index=2,
            total_chunks=5,
            payload=b"\x00\xff" * 20,
        )
        encoded = c.encode()
        assert encoded.startswith("PCHK:1:abc123:")
        decoded = Chunk.decode(encoded)
        assert decoded == c

    def test_invalid_header_raises(self):
        with pytest.raises(ValueError, match="invalid chunk header"):
            Chunk.decode("GARBAGE:stuff")


class TestChunkify:
    def test_single_chunk(self):
        data = b"small"
        chunks = chunkify(data, "m1", 0, max_chunk_bytes=512)
        assert len(chunks) == 1
        assert chunks[0].total_chunks == 1
        assert chunks[0].payload == data

    def test_splits_evenly(self):
        data = b"a" * 300
        # Force small chunks: header ~64 chars, base64 of N bytes = ceil(N/3)*4 chars
        # With max_chunk_bytes=128: usable=64 chars, raw_per_chunk=48 bytes
        chunks = chunkify(data, "m2", 0, max_chunk_bytes=128)
        assert len(chunks) > 1
        for i, c in enumerate(chunks):
            assert c.chunk_index == i
            assert c.total_chunks == len(chunks)

    def test_max_too_small_raises(self):
        with pytest.raises(ValueError):
            chunkify(b"data", "m", 0, max_chunk_bytes=10)


class TestReassemble:
    def test_round_trip(self):
        data = b"hello world this is a test of chunking"
        chunks = chunkify(data, "m1", 0, max_chunk_bytes=100)
        assert len(chunks) > 1
        result = reassemble(chunks)
        assert result == data

    def test_order_independent(self):
        data = b"order should not matter" * 10
        chunks = chunkify(data, "m1", 0, max_chunk_bytes=100)
        # Reverse the list — reassemble should handle any order
        result = reassemble(list(reversed(chunks)))
        assert result == data

    def test_missing_chunk_raises(self):
        data = b"x" * 200
        chunks = chunkify(data, "m1", 0, max_chunk_bytes=100)
        assert len(chunks) > 1
        with pytest.raises(ValueError, match="missing chunk"):
            reassemble(chunks[:-1])  # drop the last chunk

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="no chunks"):
            reassemble([])
