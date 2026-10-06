"""Chunker — split ciphertext blocks into adapter-sized pieces.

Each chunk carries a small header so it can be independently verified
and reassembled without any external index.

Wire format (text-safe, designed for post bodies):

    PCHK:<version>:<manifest_id>:<block_index>:<chunk_index>:<total_chunks>:<base64url_payload>

Everything is ASCII-safe so it survives copy-paste, post bodies, and
text-only protocol fields.
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass

_VERSION = 1
_HEADER_OVERHEAD = 64  # conservative estimate for the non-payload portion


@dataclass(frozen=True, slots=True)
class Chunk:
    """A single tagged chunk ready to be handed to an adapter."""

    version: int
    manifest_id: str
    block_index: int
    chunk_index: int
    total_chunks: int
    payload: bytes  # raw ciphertext slice

    def encode(self) -> str:
        """Serialize to the wire format string."""
        b64 = base64.urlsafe_b64encode(self.payload).decode("ascii")
        return (
            f"PCHK:{self.version}:{self.manifest_id}"
            f":{self.block_index}:{self.chunk_index}"
            f":{self.total_chunks}:{b64}"
        )

    @classmethod
    def decode(cls, text: str) -> "Chunk":
        """Deserialize a wire-format string back into a Chunk."""
        parts = text.strip().split(":")
        if len(parts) != 7 or parts[0] != "PCHK":
            raise ValueError(f"invalid chunk header: {text[:40]!r}")
        return cls(
            version=int(parts[1]),
            manifest_id=parts[2],
            block_index=int(parts[3]),
            chunk_index=int(parts[4]),
            total_chunks=int(parts[5]),
            payload=base64.urlsafe_b64decode(parts[6]),
        )


def chunkify(
    ciphertext: bytes,
    manifest_id: str,
    block_index: int,
    max_chunk_bytes: int,
) -> list[Chunk]:
    """Split *ciphertext* into chunks that each fit in *max_chunk_bytes*.

    *max_chunk_bytes* is the **total** encoded size limit (header +
    base64 payload) so the chunk is guaranteed to fit in a single
    adapter write.
    """
    # base64 expands 3 bytes → 4 chars; account for header overhead
    usable = max_chunk_bytes - _HEADER_OVERHEAD
    if usable <= 0:
        raise ValueError("max_chunk_bytes too small to fit any payload")
    # raw bytes we can pack per chunk (inverse of base64 expansion)
    raw_per_chunk = (usable * 3) // 4
    if raw_per_chunk <= 0:
        raise ValueError("max_chunk_bytes too small after base64 overhead")

    total = math.ceil(len(ciphertext) / raw_per_chunk) if ciphertext else 1
    chunks: list[Chunk] = []
    for i in range(total):
        start = i * raw_per_chunk
        end = start + raw_per_chunk
        chunks.append(
            Chunk(
                version=_VERSION,
                manifest_id=manifest_id,
                block_index=block_index,
                chunk_index=i,
                total_chunks=total,
                payload=ciphertext[start:end],
            )
        )
    return chunks


def reassemble(chunks: list[Chunk]) -> bytes:
    """Reassemble sorted chunks back into the original ciphertext block.

    Raises if any chunks are missing.
    """
    if not chunks:
        raise ValueError("no chunks to reassemble")
    total = chunks[0].total_chunks
    by_index = {c.chunk_index: c for c in chunks}
    missing = [i for i in range(total) if i not in by_index]
    if missing:
        raise ValueError(f"missing chunk indices: {missing}")
    return b"".join(by_index[i].payload for i in range(total))
