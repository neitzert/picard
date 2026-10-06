"""Adapter contract — the interface every backend must implement.

An adapter knows exactly two things:
  1. How to place a chunk of data into its protocol.
  2. How to retrieve chunks back out.

It does not manage encryption, track manifests, or understand what
the data means.  It just offers shelter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    """Capabilities and limits of an adapter.

    The orchestrator reads these to decide how to split work across
    available adapters.
    """

    name: str
    max_chunk_bytes: int  # largest encoded chunk the backend accepts
    persistent: bool = True  # chunks survive across sessions
    ordered: bool = True  # retrieval order matches write order
    ttl_seconds: int | None = None  # None = no automatic expiry
    rate_limit_per_min: int = 60  # writes per minute
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredChunkRef:
    """A reference to a chunk that was successfully placed."""

    adapter_name: str
    location: str  # adapter-specific identifier (URI, key, id, …)


class Adapter(ABC):
    """Abstract base class for all adapters."""

    @abstractmethod
    def profile(self) -> AdapterProfile:
        """Return this adapter's capability profile."""

    @abstractmethod
    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        """Place one encoded chunk string.  Return its location ref."""

    @abstractmethod
    async def read_chunk(self, ref: StoredChunkRef) -> str:
        """Retrieve the encoded chunk string at *ref*."""

    @abstractmethod
    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        """Remove the chunk at *ref*.  Return True if it existed."""

    @abstractmethod
    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """List all stored chunks belonging to *manifest_id*.

        Used for scan-based recovery when the local index is lost.
        """

    async def health_check(self) -> bool:
        """Return True if the backend is currently reachable."""
        return True
