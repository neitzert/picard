"""Tests for picard.erasure -- Reed-Solomon encode/decode."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

zfec = pytest.importorskip("zfec", reason="zfec not installed; skip erasure tests")

from picard.erasure import decode, encode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drop(shards: list[bytes], indices: list[int]) -> list[bytes | None]:
    """Return a copy of shards with the given indices set to None."""
    result: list[bytes | None] = list(shards)
    for i in indices:
        result[i] = None
    return result


# ---------------------------------------------------------------------------
# Encode / decode roundtrips
# ---------------------------------------------------------------------------

def test_encode_decode_roundtrip():
    data = b"Hello, PICARD erasure coding!"
    shards = encode(data, k=3, m=5)
    assert len(shards) == 5
    assert all(isinstance(s, bytes) for s in shards)
    recovered = decode(list(shards), k=3, m=5)
    assert recovered == data


def test_exact_k_shards():
    data = os.urandom(256)
    shards = encode(data, k=4, m=6)
    # Drop the last 2 shards (indices 4, 5) -- exactly k=4 remain.
    partial: list[bytes | None] = list(shards[:4]) + [None, None]
    assert decode(partial, k=4, m=6) == data


def test_below_k_shards_raises():
    data = b"not enough shards"
    shards = encode(data, k=4, m=6)
    partial: list[bytes | None] = list(shards[:3]) + [None, None, None]
    with pytest.raises(ValueError, match="requires 4 shards"):
        decode(partial, k=4, m=6)


# ---------------------------------------------------------------------------
# Padding edge cases
# ---------------------------------------------------------------------------

def test_padding_empty():
    shards = encode(b"", k=3, m=5)
    assert decode(list(shards), k=3, m=5) == b""


def test_padding_one_byte():
    shards = encode(b"\x42", k=3, m=5)
    assert decode(list(shards), k=3, m=5) == b"\x42"


def test_padding_k_minus_one_bytes():
    k = 4
    data = os.urandom(k - 1)
    shards = encode(data, k=k, m=6)
    assert decode(list(shards), k=k, m=6) == data


def test_padding_k_bytes():
    k = 4
    data = os.urandom(k)
    shards = encode(data, k=k, m=6)
    assert decode(list(shards), k=k, m=6) == data


def test_padding_large():
    data = os.urandom(65536)
    shards = encode(data, k=4, m=6)
    assert decode(list(shards), k=4, m=6) == data


# ---------------------------------------------------------------------------
# Any k-subset recovers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("k,m,drop", [
    (3, 5, [4]),
    (3, 5, [0]),
    (3, 5, [1, 2]),
    (4, 6, [4, 5]),
    (4, 6, [0, 5]),
    (2, 4, [2, 3]),
])
def test_any_k_subset_recovers(k, m, drop):
    data = os.urandom(128)
    shards = encode(data, k=k, m=m)
    partial = _drop(list(shards), drop)
    assert decode(partial, k=k, m=m) == data


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_config_k_ge_m_raises():
    with pytest.raises(ValueError, match="1 <= k < m"):
        encode(b"test", k=4, m=4)


def test_config_k_zero_raises():
    with pytest.raises(ValueError, match="1 <= k < m"):
        encode(b"test", k=0, m=4)


# ---------------------------------------------------------------------------
# Various sizes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("size", [1, 7, 100, 1024, 32768])
def test_encode_various_sizes(size):
    data = os.urandom(size)
    shards = encode(data, k=3, m=5)
    assert decode(list(shards), k=3, m=5) == data


# ---------------------------------------------------------------------------
# Import error
# ---------------------------------------------------------------------------

def test_zfec_import_error():
    with patch("picard.erasure._ZFEC_AVAILABLE", False):
        with pytest.raises(ImportError, match="pip install"):
            encode(b"test", k=3, m=5)

    with patch("picard.erasure._ZFEC_AVAILABLE", False):
        with pytest.raises(ImportError, match="pip install"):
            decode([None] * 5, k=3, m=5)


# ---------------------------------------------------------------------------
# RaidPool integration (mocked adapters)
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, MagicMock

from picard.adapters.base import AdapterProfile, StoredChunkRef
from picard.raid import RaidLevel, RaidPool


def _make_adapter(name: str) -> MagicMock:
    store: dict[str, str] = {}
    a = MagicMock()
    a.profile.return_value = AdapterProfile(
        name=name, max_chunk_bytes=1_000_000, persistent=True
    )

    async def write(chunk: str) -> StoredChunkRef:
        key = f"{name}:{len(store)}"
        store[key] = chunk
        return StoredChunkRef(adapter_name=name, location=key)

    async def read(ref: StoredChunkRef) -> str:
        if ref.location not in store:
            raise KeyError(ref.location)
        return store[ref.location]

    async def health() -> bool:
        return True

    a.write_chunk = write
    a.read_chunk = read
    a.health_check = health
    return a, store


def test_raidpool_erasure_write_read():
    adapters = [_make_adapter(f"a{i}")[0] for i in range(6)]
    pool = RaidPool(adapters, level=RaidLevel.ERASURE, erasure_k=4, erasure_m=6)
    chunk = "PCHK:2:test:0:0:1:" + "A" * 100

    async def run():
        ref = await pool.write_chunk(chunk)
        return await pool.read_chunk(ref)

    result = asyncio.run(run())
    assert result == chunk


def test_raidpool_erasure_partial_recovery():
    """Two of six adapters fail; k=4 means recovery should still succeed."""
    adapter_list = [_make_adapter(f"b{i}") for i in range(6)]
    adapters = [a for a, _ in adapter_list]
    stores = [s for _, s in adapter_list]

    pool = RaidPool(adapters, level=RaidLevel.ERASURE, erasure_k=4, erasure_m=6)
    chunk = "PCHK:2:test:0:0:1:" + "B" * 200

    async def run():
        ref = await pool.write_chunk(chunk)
        # Corrupt two adapter stores to simulate failure.
        stores[0].clear()
        stores[5].clear()
        return await pool.read_chunk(ref)

    result = asyncio.run(run())
    assert result == chunk
