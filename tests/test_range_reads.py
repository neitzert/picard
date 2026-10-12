"""Tests for partial/range reads and get_block."""

import asyncio

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.cache import BlockCache
from picard.index import Index
from picard.orchestrator import Orchestrator


def _make_orch(block_size=16, block_cache=None):
    adapter = MemoryAdapter()
    index = Index()
    return Orchestrator(
        [adapter], index, block_size=block_size, block_cache=block_cache,
    )


PASSPHRASE = "test-passphrase"


class TestGetBlock:
    def test_single_block_file(self):
        orch = _make_orch(block_size=1024)
        data = b"hello world"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        block = asyncio.run(orch.get_block("/f.txt", 0, PASSPHRASE))
        assert block == data

    def test_multi_block_individual(self):
        orch = _make_orch(block_size=8)
        data = b"AAAAAAAABBBBBBBBCCCC"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        assert asyncio.run(orch.get_block("/f.txt", 0, PASSPHRASE)) == b"AAAAAAAA"
        assert asyncio.run(orch.get_block("/f.txt", 1, PASSPHRASE)) == b"BBBBBBBB"
        assert asyncio.run(orch.get_block("/f.txt", 2, PASSPHRASE)) == b"CCCC"

    def test_invalid_block_index_raises(self):
        orch = _make_orch(block_size=1024)
        asyncio.run(orch.put("/f.txt", b"data", PASSPHRASE))
        with pytest.raises(IndexError):
            asyncio.run(orch.get_block("/f.txt", 99, PASSPHRASE))

    def test_missing_file_raises(self):
        orch = _make_orch()
        with pytest.raises(FileNotFoundError):
            asyncio.run(orch.get_block("/nope", 0, PASSPHRASE))


class TestReadRange:
    def test_full_file(self):
        orch = _make_orch(block_size=8)
        data = b"0123456789abcdef"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 0, len(data), PASSPHRASE))
        assert result == data

    def test_within_single_block(self):
        orch = _make_orch(block_size=16)
        data = b"0123456789abcdef"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 4, 5, PASSPHRASE))
        assert result == b"45678"

    def test_cross_boundary(self):
        orch = _make_orch(block_size=8)
        data = b"AAAAAAAABBBBBBBB"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 6, 4, PASSPHRASE))
        assert result == b"AABB"

    def test_last_block_partial(self):
        orch = _make_orch(block_size=8)
        data = b"AAAAAAAABBB"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 8, 100, PASSPHRASE))
        assert result == b"BBB"

    def test_offset_past_eof(self):
        orch = _make_orch(block_size=8)
        data = b"short"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 1000, 10, PASSPHRASE))
        assert result == b""

    def test_read_first_byte(self):
        orch = _make_orch(block_size=8)
        data = b"XYZZY"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 0, 1, PASSPHRASE))
        assert result == b"X"

    def test_read_last_byte(self):
        orch = _make_orch(block_size=8)
        data = b"XYZZY"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 4, 1, PASSPHRASE))
        assert result == b"Y"

    def test_three_block_span(self):
        orch = _make_orch(block_size=4)
        data = b"AAAABBBBCCCCDDDD"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        result = asyncio.run(orch.read_range("/f.txt", 2, 10, PASSPHRASE))
        assert result == b"AABBBBCCCC"[:10]

    def test_missing_file_raises(self):
        orch = _make_orch()
        with pytest.raises(FileNotFoundError):
            asyncio.run(orch.read_range("/nope", 0, 10, PASSPHRASE))


class TestGetRefactored:
    def test_get_still_works(self):
        orch = _make_orch(block_size=8)
        data = b"the quick brown fox"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        assert asyncio.run(orch.get("/f.txt", PASSPHRASE)) == data

    def test_get_empty_file(self):
        orch = _make_orch(block_size=8)
        asyncio.run(orch.put("/f.txt", b"", PASSPHRASE))
        assert asyncio.run(orch.get("/f.txt", PASSPHRASE)) == b""


class TestBlockCacheIntegration:
    def test_cache_hit_avoids_adapter(self):
        cache = BlockCache()
        orch = _make_orch(block_size=1024, block_cache=cache)
        data = b"cached data"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        r1 = asyncio.run(orch.get("/f.txt", PASSPHRASE))
        assert r1 == data
        assert cache.stats["misses"] == 1
        assert cache.stats["hits"] == 0
        r2 = asyncio.run(orch.get("/f.txt", PASSPHRASE))
        assert r2 == data
        assert cache.stats["hits"] == 1

    def test_cache_populated_by_get_block(self):
        cache = BlockCache()
        orch = _make_orch(block_size=8, block_cache=cache)
        data = b"AAAAAAAABBBBBBBB"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        asyncio.run(orch.get_block("/f.txt", 0, PASSPHRASE))
        assert cache.stats["entries"] == 1

    def test_cache_used_by_read_range(self):
        cache = BlockCache()
        orch = _make_orch(block_size=8, block_cache=cache)
        data = b"AAAAAAAABBBBBBBB"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        asyncio.run(orch.read_range("/f.txt", 0, 8, PASSPHRASE))
        assert cache.stats["misses"] == 1
        asyncio.run(orch.read_range("/f.txt", 0, 8, PASSPHRASE))
        assert cache.stats["hits"] == 1

    def test_no_cache_still_works(self):
        orch = _make_orch(block_size=8, block_cache=None)
        data = b"no cache here"
        asyncio.run(orch.put("/f.txt", data, PASSPHRASE))
        assert asyncio.run(orch.get("/f.txt", PASSPHRASE)) == data
        assert asyncio.run(orch.read_range("/f.txt", 0, 5, PASSPHRASE)) == b"no ca"
