"""In-memory adapter — stores chunks in a dict.  For tests only."""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MemoryAdapter(Adapter):
    """Chunks live in ``self.store`` for the lifetime of the process."""

    def __init__(self, *, name: str = "memory", max_chunk_bytes: int = 1024) -> None:
        self.store: dict[str, str] = {}  # location -> encoded chunk
        self._name = name
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=self._name,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=True,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        loc = f"mem://{self._name}/{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        self.store[loc] = encoded_chunk
        return StoredChunkRef(adapter_name=self._name, location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self.store.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self.store.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"mem://{self._name}/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name=self._name, location=loc)
            for loc in self.store
            if loc.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        return True
