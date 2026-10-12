"""Concurrency safety tests for Index write_lock, BlockCache, DerivedKeyCache, RAID stripe_lock."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.cache import BlockCache
from picard.crypto import DerivedKeyCache, decrypt, encrypt, set_key_cache
from picard.index import Index
from picard.orchestrator import Orchestrator
from picard.raid import RaidLevel, RaidPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(tmp_path: Path, n_adapters: int = 2) -> Orchestrator:
    adapters = [MemoryAdapter(name=f"mem-{i}", max_chunk_bytes=8192) for i in range(n_adapters)]
    idx = Index(tmp_path / "index.db")
    cache = BlockCache(max_entries=64, max_bytes=1 * 1024 * 1024, ttl_seconds=30)
    return Orchestrator(adapters, idx, block_size=256, block_cache=cache)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Orchestrator: parallel puts to different paths
# ---------------------------------------------------------------------------

class TestParallelPutsDifferentPaths:
    def test_concurrent_puts_succeed(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        pw = "test-pass"
        n = 10
        errors: list[Exception] = []

        def put_one(i: int):
            try:
                _run_async(orch.put(f"/file-{i}.txt", f"content-{i}".encode() * 50, pw))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=put_one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during parallel puts: {errors}"
        for i in range(n):
            data = _run_async(orch.get(f"/file-{i}.txt", pw))
            assert data == f"content-{i}".encode() * 50


# ---------------------------------------------------------------------------
# Orchestrator: parallel puts to same path (race)
# ---------------------------------------------------------------------------

class TestParallelPutsSamePath:
    def test_last_writer_wins(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        pw = "test-pass"
        n = 5
        errors: list[Exception] = []

        def put_one(i: int):
            try:
                _run_async(orch.put("/race.txt", f"version-{i}".encode() * 50, pw))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=put_one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during same-path puts: {errors}"
        data = _run_async(orch.get("/race.txt", pw))
        assert data.startswith(b"version-")


# ---------------------------------------------------------------------------
# Orchestrator: parallel gets
# ---------------------------------------------------------------------------

class TestParallelGets:
    def test_concurrent_reads_consistent(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        pw = "test-pass"
        content = b"read-me-concurrently" * 100
        _run_async(orch.put("/read-target.txt", content, pw))

        async def gather_gets():
            return await asyncio.gather(
                *[orch.get("/read-target.txt", pw) for _ in range(8)]
            )

        results = _run_async(gather_gets())
        assert len(results) == 8
        for r in results:
            assert r == content


# ---------------------------------------------------------------------------
# Orchestrator: rm + get race
# ---------------------------------------------------------------------------

class TestRmGetRace:
    def test_rm_during_get_no_crash(self, tmp_path):
        orch = _make_orchestrator(tmp_path)
        pw = "test-pass"
        content = b"ephemeral" * 100
        _run_async(orch.put("/ephemeral.txt", content, pw))

        errors: list[Exception] = []
        get_results: list[bytes | None] = [None]

        def do_get():
            try:
                get_results[0] = _run_async(orch.get("/ephemeral.txt", pw))
            except (FileNotFoundError, KeyError):
                get_results[0] = None
            except Exception as e:
                errors.append(e)

        def do_rm():
            try:
                _run_async(orch.rm("/ephemeral.txt"))
            except Exception as e:
                errors.append(e)

        t_get = threading.Thread(target=do_get)
        t_rm = threading.Thread(target=do_rm)
        t_get.start()
        t_rm.start()
        t_get.join(timeout=30)
        t_rm.join(timeout=30)

        assert not errors, f"Unexpected errors during rm+get race: {errors}"


# ---------------------------------------------------------------------------
# BlockCache thread safety
# ---------------------------------------------------------------------------

class TestBlockCacheThreadSafety:
    def test_concurrent_put_get(self):
        cache = BlockCache(max_entries=64, max_bytes=1 * 1024 * 1024, ttl_seconds=30)
        n_threads = 8
        n_ops = 50
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                for i in range(n_ops):
                    data = os.urandom(128)
                    cache.put(f"m-{tid}", i, data)
                    result = cache.get(f"m-{tid}", i)
                    assert result == data, f"Cache returned wrong data for m-{tid} block {i}"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"BlockCache thread safety errors: {errors}"
        stats = cache.stats
        assert stats["entries"] > 0
        assert stats["bytes"] > 0

    def test_concurrent_put_invalidate(self):
        cache = BlockCache(max_entries=64, max_bytes=1 * 1024 * 1024, ttl_seconds=30)
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(100):
                    cache.put("manifest-1", i % 10, os.urandom(64))
            except Exception as e:
                errors.append(e)

        def invalidator():
            try:
                for _ in range(50):
                    cache.invalidate("manifest-1")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=invalidator)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"BlockCache put/invalidate race errors: {errors}"

    def test_concurrent_clear(self):
        cache = BlockCache(max_entries=64, max_bytes=1 * 1024 * 1024, ttl_seconds=30)
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(200):
                    cache.put("m", i, os.urandom(32))
            except Exception as e:
                errors.append(e)

        def clearer():
            try:
                for _ in range(20):
                    cache.clear()
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=clearer)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors


# ---------------------------------------------------------------------------
# DerivedKeyCache thread safety
# ---------------------------------------------------------------------------

class TestDerivedKeyCacheThreadSafety:
    def test_concurrent_get_put(self):
        cache = DerivedKeyCache(max_entries=16, ttl_seconds=30)
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                for i in range(20):
                    salt = os.urandom(16)
                    raw = os.urandom(32)
                    pw = f"pass-{tid}-{i}"
                    cipher = cache.put(pw, salt, raw)
                    assert cipher is not None
                    result = cache.get(pw, salt)
                    if result is not None:
                        got_raw, got_cipher = result
                        assert got_raw == raw
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"DerivedKeyCache thread safety errors: {errors}"

    def test_concurrent_encrypt_decrypt(self):
        cache = DerivedKeyCache(max_entries=16, ttl_seconds=30)
        old_cache = None
        try:
            from picard import crypto
            old_cache = crypto._key_cache
            set_key_cache(cache)

            errors: list[Exception] = []
            plaintext = b"concurrent-crypto-test" * 10

            def worker(tid: int):
                try:
                    pw = f"thread-{tid}-pass"
                    for _ in range(10):
                        ct = encrypt(plaintext, pw)
                        pt = decrypt(ct, pw)
                        assert pt == plaintext
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert not errors, f"Concurrent encrypt/decrypt errors: {errors}"
        finally:
            set_key_cache(old_cache)

    def test_eviction_under_contention(self):
        cache = DerivedKeyCache(max_entries=4, ttl_seconds=30)
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                for i in range(30):
                    salt = os.urandom(16)
                    raw = os.urandom(32)
                    cache.put(f"pw-{tid}-{i}", salt, raw)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        cache.clear()


# ---------------------------------------------------------------------------
# RAID stripe lock thread safety
# ---------------------------------------------------------------------------

class TestRaidStripeLock:
    def test_concurrent_raid0_writes(self):
        adapters = [MemoryAdapter(name=f"r-{i}", max_chunk_bytes=8192) for i in range(3)]
        pool = RaidPool(adapters, RaidLevel.RAID0)
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(10):
                    from picard.chunker import Chunk
                    chunk = Chunk(
                        version=1,
                        manifest_id=f"m{tid}",
                        block_index=i,
                        chunk_index=0,
                        total_chunks=1,
                        payload=os.urandom(64),
                    )
                    _run_async(pool.write_chunk(chunk.encode()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"RAID0 concurrent write errors: {errors}"

    def test_concurrent_raid1_writes(self):
        adapters = [MemoryAdapter(name=f"r1-{i}", max_chunk_bytes=8192) for i in range(3)]
        pool = RaidPool(adapters, RaidLevel.RAID1)
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(10):
                    from picard.chunker import Chunk
                    chunk = Chunk(
                        version=1,
                        manifest_id=f"m{tid}",
                        block_index=i,
                        chunk_index=0,
                        total_chunks=1,
                        payload=os.urandom(64),
                    )
                    _run_async(pool.write_chunk(chunk.encode()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors

    def test_concurrent_raid5_writes(self):
        adapters = [MemoryAdapter(name=f"r5-{i}", max_chunk_bytes=8192) for i in range(3)]
        pool = RaidPool(adapters, RaidLevel.RAID5)
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(10):
                    from picard.chunker import Chunk
                    chunk = Chunk(
                        version=1,
                        manifest_id=f"m{tid}",
                        block_index=i,
                        chunk_index=0,
                        total_chunks=1,
                        payload=os.urandom(64),
                    )
                    _run_async(pool.write_chunk(chunk.encode()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors


# ---------------------------------------------------------------------------
# Index write_lock: concurrent save_manifest
# ---------------------------------------------------------------------------

class TestIndexWriteLock:
    def test_concurrent_save_manifest(self, tmp_path):
        from picard.manifest import Manifest
        idx = Index(tmp_path / "index.db")
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(20):
                    m = Manifest(
                        path=f"/t{tid}/file-{i}.txt",
                        original_size=100,
                        block_size=256,
                        total_blocks=0,
                        created_at="2026-01-01T00:00:00Z",
                        plaintext_hash="abc",
                        parent_path=f"/t{tid}",
                        version=1,
                    )
                    idx.save_manifest(m)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Index concurrent save_manifest errors: {errors}"
        for tid in range(8):
            for i in range(20):
                m = idx.get_manifest_by_path(f"/t{tid}/file-{i}.txt")
                assert m is not None

    def test_concurrent_delete_manifest(self, tmp_path):
        from picard.manifest import Manifest
        idx = Index(tmp_path / "index.db")

        manifests = []
        for i in range(40):
            m = Manifest(
                path=f"/del/file-{i}.txt",
                original_size=50,
                block_size=256,
                total_blocks=0,
                created_at="2026-01-01T00:00:00Z",
                plaintext_hash="xyz",
                parent_path="/del",
                version=1,
            )
            idx.save_manifest(m)
            manifests.append(m)

        errors: list[Exception] = []

        def deleter(start: int, end: int):
            try:
                for i in range(start, end):
                    idx.delete_manifest(manifests[i].manifest_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=deleter, args=(0, 10)),
            threading.Thread(target=deleter, args=(10, 20)),
            threading.Thread(target=deleter, args=(20, 30)),
            threading.Thread(target=deleter, args=(30, 40)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Index concurrent delete errors: {errors}"
        for m in manifests:
            assert idx.get_manifest_by_path(m.path) is None
