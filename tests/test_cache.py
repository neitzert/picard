"""Tests for the block cache."""

import threading
import time

from picard.cache import BlockCache


class TestBlockCacheBasic:
    def test_put_and_get(self):
        cache = BlockCache()
        cache.put("m1", 0, b"hello")
        assert cache.get("m1", 0) == b"hello"

    def test_miss_returns_none(self):
        cache = BlockCache()
        assert cache.get("missing", 0) is None

    def test_stats_tracking(self):
        cache = BlockCache()
        cache.put("m1", 0, b"data")
        cache.get("m1", 0)
        cache.get("m1", 99)
        s = cache.stats
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["entries"] == 1
        assert s["bytes"] == 4

    def test_clear(self):
        cache = BlockCache()
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        cache.clear()
        assert cache.get("m1", 0) is None
        assert cache.stats["entries"] == 0
        assert cache.stats["bytes"] == 0


class TestBlockCacheEviction:
    def test_lru_max_entries(self):
        cache = BlockCache(max_entries=2)
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        cache.put("m1", 2, b"c")
        assert cache.get("m1", 0) is None
        assert cache.get("m1", 1) == b"b"
        assert cache.get("m1", 2) == b"c"

    def test_lru_promotes_on_get(self):
        cache = BlockCache(max_entries=2)
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        cache.get("m1", 0)
        cache.put("m1", 2, b"c")
        assert cache.get("m1", 0) == b"a"
        assert cache.get("m1", 1) is None

    def test_max_bytes_eviction(self):
        cache = BlockCache(max_entries=100, max_bytes=10)
        cache.put("m1", 0, b"12345")
        cache.put("m1", 1, b"67890")
        assert cache.stats["bytes"] == 10
        cache.put("m1", 2, b"abcde")
        assert cache.get("m1", 0) is None
        assert cache.stats["bytes"] <= 10

    def test_ttl_expiry(self):
        cache = BlockCache(ttl_seconds=0.05)
        cache.put("m1", 0, b"data")
        assert cache.get("m1", 0) == b"data"
        time.sleep(0.08)
        assert cache.get("m1", 0) is None

    def test_eviction_counter(self):
        cache = BlockCache(max_entries=1)
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        assert cache.stats["evictions"] >= 1


class TestBlockCacheInvalidate:
    def test_invalidate_single(self):
        cache = BlockCache()
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        cache.invalidate("m1", 0)
        assert cache.get("m1", 0) is None
        assert cache.get("m1", 1) == b"b"

    def test_invalidate_all_for_manifest(self):
        cache = BlockCache()
        cache.put("m1", 0, b"a")
        cache.put("m1", 1, b"b")
        cache.put("m2", 0, b"c")
        cache.invalidate("m1")
        assert cache.get("m1", 0) is None
        assert cache.get("m1", 1) is None
        assert cache.get("m2", 0) == b"c"

    def test_invalidate_nonexistent_is_noop(self):
        cache = BlockCache()
        cache.invalidate("nope", 0)
        cache.invalidate("nope")


class TestBlockCacheOverwrite:
    def test_put_replaces_existing(self):
        cache = BlockCache()
        cache.put("m1", 0, b"old")
        cache.put("m1", 0, b"new")
        assert cache.get("m1", 0) == b"new"
        assert cache.stats["entries"] == 1

    def test_put_updates_byte_count(self):
        cache = BlockCache()
        cache.put("m1", 0, b"123")
        assert cache.stats["bytes"] == 3
        cache.put("m1", 0, b"12345678")
        assert cache.stats["bytes"] == 8


class TestBlockCacheThreadSafety:
    def test_concurrent_access(self):
        cache = BlockCache(max_entries=100)
        errors: list[str] = []

        def writer(tid: int) -> None:
            try:
                for i in range(50):
                    cache.put(f"m{tid}", i, f"data-{tid}-{i}".encode())
            except Exception as e:
                errors.append(str(e))

        def reader(tid: int) -> None:
            try:
                for i in range(50):
                    cache.get(f"m{tid}", i)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for t in range(4):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
