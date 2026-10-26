"""Reed-Solomon erasure coding for PICARD scatter pools.

Wraps ``zfec`` to provide an encode/decode API suitable for the RAID layer.
Any k of m shards reconstruct the original data, tolerating m-k adapter
failures with (m/k - 1) storage overhead.

Optional dependency: install with ``pip install 'picard-store[erasure]'``.
"""

from __future__ import annotations

try:
    import zfec
    _ZFEC_AVAILABLE = True
except ImportError:
    _ZFEC_AVAILABLE = False
    zfec = None  # type: ignore[assignment]


def _check_zfec() -> None:
    if not _ZFEC_AVAILABLE:
        raise ImportError(
            "zfec is required for erasure coding: "
            "pip install 'picard-store[erasure]'"
        )


def encode(data: bytes, k: int, m: int) -> list[bytes]:
    """Split *data* into *m* shards; any *k* of them reconstruct the original.

    Parameters
    ----------
    data:
        Raw bytes to protect.
    k:
        Number of data shards (minimum needed for recovery).  Must satisfy
        1 <= k < m.
    m:
        Total shards (k data shards plus m-k parity shards).

    Returns a list of *m* equal-length ``bytes`` objects.

    Implementation note: a big-endian 8-byte length prefix is prepended to
    *data* before splitting so that ``decode`` can strip zero-padding added
    to align the block to a multiple of *k* bytes.
    """
    _check_zfec()
    if not (1 <= k < m):
        raise ValueError(f"erasure encode requires 1 <= k < m, got k={k} m={m}")

    prefixed = len(data).to_bytes(8, "big") + data
    pad = (-len(prefixed)) % k
    prefixed += b"\x00" * pad
    block_size = len(prefixed) // k
    blocks = [prefixed[i * block_size : (i + 1) * block_size] for i in range(k)]
    enc = zfec.Encoder(k, m)
    return list(enc.encode(blocks))


def decode(shards: list[bytes | None], k: int, m: int) -> bytes:
    """Reconstruct original data from at least *k* of *m* shards.

    Parameters
    ----------
    shards:
        List of exactly *m* elements.  Present shards are ``bytes``; missing
        shards (failed or unavailable adapters) are ``None``.
    k:
        Data shard count used during ``encode``.
    m:
        Total shard count used during ``encode``.

    Raises ``ValueError`` if fewer than *k* shards are available.
    """
    _check_zfec()
    available = [(i, s) for i, s in enumerate(shards) if s is not None]
    if len(available) < k:
        raise ValueError(
            f"erasure decode requires {k} shards, only {len(available)} available"
        )
    indices, blocks = zip(*available[:k])
    dec = zfec.Decoder(k, m)
    decoded_blocks = dec.decode(list(blocks), list(indices))
    prefixed = b"".join(decoded_blocks)
    orig_len = int.from_bytes(prefixed[:8], "big")
    return prefixed[8 : 8 + orig_len]
