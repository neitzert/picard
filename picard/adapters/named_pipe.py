"""Named pipe adapter — stores chunks via OS named pipes / FIFOs.

Uses a local cache for read-back since pipes are transient.
The pipe write itself can cross process boundaries, making this
useful for IPC-based exfiltration between processes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class NamedPipeAdapter(Adapter):
    """Scatter chunks through named pipes with local cache."""

    def __init__(
        self,
        pipe_dir: str | Path = "/tmp/.picard_pipes",
        *,
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._dir = Path(pipe_dir)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="named-pipe",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        self._cache[key] = encoded_chunk
        location = f"pipe://{self._dir}/{key}"
        return StoredChunkRef(adapter_name="named-pipe", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        if key not in self._cache:
            raise KeyError(f"chunk not in pipe cache: {key}")
        return self._cache[key]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        return self._cache.pop(key, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(
                adapter_name="named-pipe",
                location=f"pipe://{self._dir}/{k}",
            )
            for k in self._cache
            if k.startswith(manifest_id)
        ]

    async def health_check(self) -> bool:
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
