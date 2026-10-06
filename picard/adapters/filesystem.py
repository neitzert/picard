"""Filesystem adapter — stores chunks as individual files on disk.

Useful for local testing with persistent state across runs without
needing any external service.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class FilesystemAdapter(Adapter):
    """Each chunk becomes a small text file under *root_dir*."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="filesystem",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
        )

    def _chunk_path(self, manifest_id: str, block_index: int, chunk_index: int) -> Path:
        d = self._root / manifest_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"b{block_index}_c{chunk_index}.pchk"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        p = self._chunk_path(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        p.write_text(encoded_chunk, encoding="utf-8")
        loc = str(p.resolve())
        return StoredChunkRef(adapter_name="filesystem", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        p = Path(ref.location)
        if not p.is_file():
            raise FileNotFoundError(f"chunk file not found: {ref.location}")
        return p.read_text(encoding="utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        p = Path(ref.location)
        if p.is_file():
            p.unlink()
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        d = self._root / manifest_id
        if not d.is_dir():
            return []
        return [
            StoredChunkRef(adapter_name="filesystem", location=str(f.resolve()))
            for f in sorted(d.glob("*.pchk"))
        ]

    async def health_check(self) -> bool:
        return self._root.is_dir() and os.access(self._root, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        usage = shutil.disk_usage(self._root)
        chunk_count = sum(1 for _ in self._root.rglob("*.pchk"))
        used = sum(f.stat().st_size for f in self._root.rglob("*.pchk"))
        return {
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "chunk_count": chunk_count,
        }
