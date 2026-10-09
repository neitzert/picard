"""Tests for data corruption detection, truncation, missing chunks, and recovery.

Verifies PICARD correctly detects and reports corrupted, truncated, or
missing data at each layer: crypto, chunker, envelope, and orchestrator.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.cache import BlockCache
from picard.chunker import Chunk, chunkify, reassemble
from picard.crypto import (
    _ALG_AES256GCM,
    _FORMAT_V2,
    _HEADER_V2,
    decrypt,
    encrypt,
)
from picard.envelope import ENVELOPE_OVERHEAD, seal, unseal
from picard.index import Index
from picard.keys import KeyRing, SubKey, derive_correlation_tag, load_master_key
from picard.manifest import BlockRecord, ChunkRecord, ChunkStatus, block_digest
from picard.orchestrator import Orchestrator


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Crypto layer corruption
# ---------------------------------------------------------------------------


class TestCryptoCorruption:
    def test_flipped_bit_detected(self):
        ct = encrypt(b"secret data", "pw")
        corrupted = bytearray(ct)
        corrupted[-5] ^= 0x01
        with pytest.raises(Exception):
            decrypt(bytes(corrupted), "pw")

    def test_truncated_ciphertext_raises(self):
        ct = encrypt(b"hello", "pw")
        with pytest.raises(Exception):
            decrypt(ct[: len(ct) // 2], "pw")

    def test_empty_blob_raises(self):
        with pytest.raises(ValueError, match="too short"):
            decrypt(b"", "pw")

    def test_random_bytes_raise(self):
        with pytest.raises(Exception):
            decrypt(os.urandom(100), "pw")

    def test_corrupted_salt_raises(self):
        ct = encrypt(b"data", "pw")
        corrupted = bytearray(ct)
        corrupted[2] ^= 0xFF
        with pytest.raises(Exception):
            decrypt(bytes(corrupted), "pw")

    def test_corrupted_nonce_raises(self):
        ct = encrypt(b"data", "pw")
        corrupted = bytearray(ct)
        corrupted[20] ^= 0xFF
        with pytest.raises(Exception):
            decrypt(bytes(corrupted), "pw")

    def test_appended_bytes_detected(self):
        ct = encrypt(b"data", "pw")
        with pytest.raises(Exception):
            decrypt(ct + b"\x00" * 16, "pw")

    def test_v2_header_with_zero_length_ct(self):
        header = _HEADER_V2.pack(_FORMAT_V2, _ALG_AES256GCM, os.urandom(16), os.urandom(12))
        with pytest.raises(Exception):
            decrypt(header, "pw")


# ---------------------------------------------------------------------------
# Chunker corruption
# ---------------------------------------------------------------------------


class TestChunkerCorruption:
    def test_missing_chunk_in_reassembly(self):
        data = os.urandom(200)
        chunks = chunkify(data, "m1", 0, 256)
        assert len(chunks) >= 1
        if len(chunks) > 1:
            with pytest.raises(ValueError, match="missing chunk"):
                reassemble(chunks[:-1])

    def test_empty_chunk_list_raises(self):
        with pytest.raises(ValueError, match="no chunks"):
            reassemble([])

    def test_invalid_wire_format_raises(self):
        with pytest.raises(ValueError, match="invalid chunk header"):
            Chunk.decode("NOT_A_PCHK_STRING")

    def test_truncated_wire_format_raises(self):
        with pytest.raises(ValueError):
            Chunk.decode("PCHK:1:abc")

    def test_corrupted_base64_payload(self):
        chunk = Chunk(
            version=1, manifest_id="m1", block_index=0,
            chunk_index=0, total_chunks=1, payload=os.urandom(64),
        )
        encoded = chunk.encode()
        parts = encoded.split(":")
        parts[-1] = "!!!invalid-base64!!!"
        with pytest.raises(Exception):
            Chunk.decode(":".join(parts))

    def test_wrong_total_chunks_still_reassembles(self):
        chunk = Chunk(
            version=1, manifest_id="m1", block_index=0,
            chunk_index=0, total_chunks=1, payload=b"hello",
        )
        result = reassemble([chunk])
        assert result == b"hello"


# ---------------------------------------------------------------------------
# Envelope corruption
# ---------------------------------------------------------------------------


class TestEnvelopeCorruption:
    def _make_key_ring(self, passphrase="test-pass"):
        mk = load_master_key(passphrase=passphrase)
        return KeyRing(mk)

    def test_truncated_envelope_raises(self):
        sub = SubKey(key=os.urandom(32), adapter_name="test", version=1)
        with pytest.raises(ValueError, match="too short"):
            unseal(b"\x01" * 10, sub)

    def test_wrong_version_raises(self):
        kr = self._make_key_ring()
        sub_key = kr.get_sub_key("adapter1")
        envelope = seal("PCHK:1:m1:0:0:1:dGVzdA==", sub_key, "m1")
        corrupted = bytearray(envelope)
        corrupted[0] = 0xFF
        with pytest.raises(ValueError, match="unsupported envelope version"):
            unseal(bytes(corrupted), sub_key)

    def test_corrupted_ciphertext_raises(self):
        kr = self._make_key_ring()
        sub_key = kr.get_sub_key("adapter1")
        envelope = seal("PCHK:1:m1:0:0:1:dGVzdA==", sub_key, "m1")
        corrupted = bytearray(envelope)
        corrupted[-3] ^= 0x01
        with pytest.raises(Exception):
            unseal(bytes(corrupted), sub_key)

    def test_wrong_key_raises(self):
        kr1 = self._make_key_ring("pass-one")
        kr2 = self._make_key_ring("pass-two")
        sub1 = kr1.get_sub_key("adapter1")
        sub2 = kr2.get_sub_key("adapter1")
        envelope = seal("PCHK:1:m1:0:0:1:dGVzdA==", sub1, "m1")
        with pytest.raises(Exception):
            unseal(envelope, sub2)

    def test_seal_unseal_roundtrip(self):
        kr = self._make_key_ring()
        sub_key = kr.get_sub_key("test-adapter")
        original = "PCHK:1:m1:0:0:1:dGVzdA=="
        envelope = seal(original, sub_key, "m1")
        assert len(envelope) > ENVELOPE_OVERHEAD
        result = unseal(envelope, sub_key)
        assert result == original


# ---------------------------------------------------------------------------
# Orchestrator corruption detection
# ---------------------------------------------------------------------------


class TestOrchestratorCorruption:
    def _make_orch(self, tmp_path):
        adapters = [MemoryAdapter(name="mem-0", max_chunk_bytes=8192)]
        idx = Index(tmp_path / "index.db")
        cache = BlockCache(max_entries=64, max_bytes=1024 * 1024, ttl_seconds=30)
        return Orchestrator(adapters, idx, block_size=256, block_cache=cache)

    def test_block_digest_mismatch_detected(self, tmp_path):
        orch = self._make_orch(tmp_path)
        pw = "test-pass"
        content = b"original content" * 20
        _run(orch.put("/file.txt", content, pw))

        adapter = orch._adapters[0]
        for loc, data in list(adapter.store.items()):
            corrupted = data[:-5] + "XXXXX"
            adapter.store[loc] = corrupted
            break

        with pytest.raises(Exception):
            _run(orch.get("/file.txt", pw))

    def test_missing_chunk_on_adapter(self, tmp_path):
        orch = self._make_orch(tmp_path)
        pw = "test-pass"
        _run(orch.put("/file.txt", b"hello world" * 30, pw))

        adapter = orch._adapters[0]
        first_key = next(iter(adapter.store))
        del adapter.store[first_key]

        with pytest.raises(Exception):
            _run(orch.get("/file.txt", pw))

    def test_verify_detects_missing_chunk(self, tmp_path):
        orch = self._make_orch(tmp_path)
        pw = "test-pass"
        _run(orch.put("/verify.txt", b"verify me" * 30, pw))

        adapter = orch._adapters[0]
        first_key = next(iter(adapter.store))
        del adapter.store[first_key]

        report = _run(orch.verify("/verify.txt"))
        assert not report["ok"]
        assert report["missing_chunks"] > 0

    def test_verify_detects_corrupted_digest(self, tmp_path):
        orch = self._make_orch(tmp_path)
        pw = "test-pass"
        _run(orch.put("/corrupt.txt", b"corrupted data" * 30, pw))

        adapter = orch._adapters[0]
        for loc, data in list(adapter.store.items()):
            chunk = Chunk.decode(data)
            bad_chunk = Chunk(
                version=chunk.version,
                manifest_id=chunk.manifest_id,
                block_index=chunk.block_index,
                chunk_index=chunk.chunk_index,
                total_chunks=chunk.total_chunks,
                payload=os.urandom(len(chunk.payload)),
            )
            adapter.store[loc] = bad_chunk.encode()
            break

        report = _run(orch.verify("/corrupt.txt"))
        assert not report["ok"]
        assert report["corrupt_blocks"] > 0

    def test_verify_passes_for_intact_file(self, tmp_path):
        orch = self._make_orch(tmp_path)
        pw = "test-pass"
        _run(orch.put("/intact.txt", b"all good" * 30, pw))

        report = _run(orch.verify("/intact.txt"))
        assert report["ok"]
        assert report["missing_chunks"] == 0
        assert report["corrupt_blocks"] == 0

    def test_verify_nonexistent_file_raises(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with pytest.raises(FileNotFoundError):
            _run(orch.verify("/nope.txt"))

    def test_get_nonexistent_raises(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with pytest.raises(FileNotFoundError):
            _run(orch.get("/nope.txt", "pw"))

    def test_get_directory_raises(self, tmp_path):
        orch = self._make_orch(tmp_path)
        _run(orch.mkdir("/mydir"))
        with pytest.raises(FileNotFoundError, match="not a regular file"):
            _run(orch.get("/mydir", "pw"))


# ---------------------------------------------------------------------------
# Block cache corruption handling
# ---------------------------------------------------------------------------


class TestBlockCacheCorruption:
    def test_expired_entry_not_returned(self):
        cache = BlockCache(max_entries=10, max_bytes=1024, ttl_seconds=0.001)
        cache.put("m1", 0, b"data")
        import time
        time.sleep(0.01)
        assert cache.get("m1", 0) is None

    def test_invalidation_clears_entry(self):
        cache = BlockCache(max_entries=10, max_bytes=1024, ttl_seconds=30)
        cache.put("m1", 0, b"data")
        cache.invalidate("m1", 0)
        assert cache.get("m1", 0) is None

    def test_invalidation_by_manifest_clears_all_blocks(self):
        cache = BlockCache(max_entries=10, max_bytes=1024, ttl_seconds=30)
        cache.put("m1", 0, b"data0")
        cache.put("m1", 1, b"data1")
        cache.put("m1", 2, b"data2")
        cache.invalidate("m1")
        assert cache.get("m1", 0) is None
        assert cache.get("m1", 1) is None
        assert cache.get("m1", 2) is None

    def test_clear_wipes_everything(self):
        cache = BlockCache(max_entries=10, max_bytes=1024, ttl_seconds=30)
        for i in range(5):
            cache.put("m1", i, b"x" * 10)
        cache.clear()
        assert cache.stats["entries"] == 0
        assert cache.stats["bytes"] == 0

    def test_eviction_on_max_entries(self):
        cache = BlockCache(max_entries=3, max_bytes=10 * 1024, ttl_seconds=30)
        for i in range(5):
            cache.put("m1", i, b"data")
        assert cache.stats["entries"] <= 3

    def test_eviction_on_max_bytes(self):
        cache = BlockCache(max_entries=100, max_bytes=100, ttl_seconds=30)
        for i in range(10):
            cache.put("m1", i, b"x" * 50)
        assert cache.stats["bytes"] <= 100


# ---------------------------------------------------------------------------
# Journal crash recovery
# ---------------------------------------------------------------------------


class TestJournalRecovery:
    def test_incomplete_put_recovered(self, tmp_path):
        adapters = [MemoryAdapter(name="mem-0", max_chunk_bytes=8192)]
        idx = Index(tmp_path / "index.db")
        from picard.journal import Journal
        journal = Journal(idx.connection, write_lock=idx.write_lock)
        orch = Orchestrator(adapters, idx, block_size=256, journal=journal)

        _run(orch.put("/recovery.txt", b"recover me" * 30, "pw"))

        jid = journal.begin("put", "/failed.txt", "fake-manifest-id")
        journal.mark_in_progress(jid, {"last_block": 0})

        incomplete = journal.get_incomplete()
        assert len(incomplete) == 1
        assert incomplete[0].path == "/failed.txt"

        result = _run(orch.recover())
        assert result["recovered"] == 1

        incomplete_after = journal.get_incomplete()
        assert len(incomplete_after) == 0

    def test_cleanup_committed_entries(self, tmp_path):
        idx = Index(tmp_path / "index.db")
        from picard.journal import Journal
        journal = Journal(idx.connection, write_lock=idx.write_lock)

        jid = journal.begin("put", "/done.txt", "m1")
        journal.commit(jid)

        cleaned = journal.cleanup_committed()
        assert cleaned == 1

        remaining = journal.get_incomplete()
        assert len(remaining) == 0
