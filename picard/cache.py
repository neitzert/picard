"""Block cache — LRU cache for decrypted plaintext blocks with TTL.

Addresses the Argon2id performance problem: each block decrypt runs a
full Argon2id pass (time_cost=3, memory_cost=64 MiB).  Without caching,
reading a 1 MiB file with 64 KiB blocks requires 16 separate derivations.
This cache ensures each block is decrypted at most once within its TTL.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


def _zeroize(b: bytearray) -> None:
    for i in range(len(b)):
        b[i] = 0


@dataclass(slots=True)
class _CacheEntry:
    data: bytearray
    expires_at: float


class BlockCache:
    """Thread-safe LRU cache for decrypted plaintext blocks.

    Security properties:
      - Plaintext lives only in process memory (never on disk)
      - TTL ensures stale data is evicted
      - Max size prevents unbounded memory growth
      - .clear() wipes all entries (call on unmount/lock)
    """

    def __init__(
        self,
        max_entries: int = 1024,
        max_bytes: int = 64 * 1024 * 1024,
        ttl_seconds: float = 300.0,
    ) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._ttl = ttl_seconds
        self._store: OrderedDict[tuple[str, int], _CacheEntry] = OrderedDict()
        self._current_bytes = 0
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, manifest_id: str, block_index: int) -> bytes | None:
        key = (manifest_id, block_index)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.monotonic() > entry.expires_at:
                _zeroize(entry.data)
                self._store.pop(key)
                self._current_bytes -= len(entry.data)
                self._evictions += 1
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return bytes(entry.data)

    def put(self, manifest_id: str, block_index: int, data: bytes) -> None:
        key = (manifest_id, block_index)
        expires = time.monotonic() + self._ttl
        with self._lock:
            if key in self._store:
                old = self._store.pop(key)
                _zeroize(old.data)
                self._current_bytes -= len(old.data)

            self._evict_expired()

            while (
                self._store
                and self._current_bytes + len(data) > self._max_bytes
            ):
                self._evict_lru()

            while self._store and len(self._store) >= self._max_entries:
                self._evict_lru()

            self._store[key] = _CacheEntry(
                data=bytearray(data), expires_at=expires
            )
            self._current_bytes += len(data)

    def invalidate(
        self, manifest_id: str, block_index: int | None = None
    ) -> None:
        with self._lock:
            if block_index is not None:
                key = (manifest_id, block_index)
                entry = self._store.pop(key, None)
                if entry is not None:
                    _zeroize(entry.data)
                    self._current_bytes -= len(entry.data)
            else:
                to_remove = [
                    k for k in self._store if k[0] == manifest_id
                ]
                for k in to_remove:
                    entry = self._store.pop(k)
                    _zeroize(entry.data)
                    self._current_bytes -= len(entry.data)

    def clear(self) -> None:
        with self._lock:
            for entry in self._store.values():
                _zeroize(entry.data)
            self._store.clear()
            self._current_bytes = 0

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._store),
                "bytes": self._current_bytes,
                "evictions": self._evictions,
            }

    def _evict_expired(self) -> None:
        now = time.monotonic()
        to_remove = [
            k for k, v in self._store.items() if now > v.expires_at
        ]
        for k in to_remove:
            entry = self._store.pop(k)
            _zeroize(entry.data)
            self._current_bytes -= len(entry.data)
            self._evictions += 1

    def _evict_lru(self) -> None:
        if self._store:
            _, entry = self._store.popitem(last=False)
            _zeroize(entry.data)
            self._current_bytes -= len(entry.data)
            self._evictions += 1
