"""Manifest — metadata model for files, blocks, and chunks.

A *manifest* is the record that maps a logical file to its encrypted
blocks and those blocks to the individual chunks scattered across
adapters.  It carries just enough information to reassemble the
original without leaking anything about the content.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ChunkStatus(str, Enum):
    """Lifecycle state of a single chunk."""

    PENDING = "pending"
    STORED = "stored"
    VERIFIED = "verified"
    MISSING = "missing"


@dataclass(slots=True)
class ChunkRecord:
    """One chunk of one block, as placed by an adapter."""

    chunk_index: int
    adapter_name: str
    location: str  # adapter-specific URI / identifier
    status: ChunkStatus = ChunkStatus.PENDING
    size_bytes: int = 0


@dataclass(slots=True)
class BlockRecord:
    """One encrypted block of the original file."""

    block_index: int
    digest: str  # SHA-256 hex of the *ciphertext* block
    size_bytes: int = 0
    chunks: list[ChunkRecord] = field(default_factory=list)


@dataclass(slots=True)
class Manifest:
    """Top-level record tying a virtual path to its scattered pieces."""

    manifest_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    path: str = ""  # virtual filesystem path
    original_size: int = 0
    block_size: int = 0
    total_blocks: int = 0
    blocks: list[BlockRecord] = field(default_factory=list)
    created_at: str = ""  # ISO-8601
    tags: dict[str, str] = field(default_factory=dict)

    # --- helpers -----------------------------------------------------------

    @property
    def total_chunks(self) -> int:
        return sum(len(b.chunks) for b in self.blocks)

    @property
    def all_stored(self) -> bool:
        return all(
            c.status in (ChunkStatus.STORED, ChunkStatus.VERIFIED)
            for b in self.blocks
            for c in b.chunks
        )


def block_digest(ciphertext: bytes) -> str:
    """SHA-256 hex digest of a ciphertext block."""
    return hashlib.sha256(ciphertext).hexdigest()
