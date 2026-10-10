"""Named pipe adapter — stores chunks via OS named pipes / FIFOs.

On Unix, creates a FIFO per chunk for IPC transfer.  On Windows,
writes to regular files in the pipe directory (no mkfifo).  A local
cache supplements read-back since pipe data is consumed on read.

The pipe write crosses process boundaries, making this useful for
IPC-based data transfer between processes.
"""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_HAS_MKFIFO = hasattr(os, "mkfifo")


class NamedPipeAdapter(Adapter):
    """Scatter chunks through named pipes with file-backed read cache."""

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
            persistent=True,
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

    def _pipe_path(self, key: str) -> Path:
        return self._dir / key

    def _sidecar_path(self, key: str) -> Path:
        return self._dir / f".{key}.dat"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        self._dir.mkdir(parents=True, exist_ok=True)

        sidecar = self._sidecar_path(key)
        await asyncio.to_thread(sidecar.write_text, encoded_chunk, "utf-8")

        if _HAS_MKFIFO:
            pipe = self._pipe_path(key)
            if not pipe.exists():
                os.mkfifo(pipe)

            def _write_fifo():
                fd = os.open(str(pipe), os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, encoded_chunk.encode())
                finally:
                    os.close(fd)

            try:
                await asyncio.to_thread(_write_fifo)
            except OSError:
                pass

        self._cache[key] = encoded_chunk
        location = f"pipe://{self._dir}/{key}"
        return StoredChunkRef(adapter_name="named-pipe", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("/", 1)[-1]
        if key in self._cache:
            return self._cache[key]

        sidecar = self._sidecar_path(key)
        if sidecar.exists():
            data = await asyncio.to_thread(sidecar.read_text, "utf-8")
            self._cache[key] = data
            return data

        raise KeyError(f"chunk not in pipe cache: {key}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("/", 1)[-1]
        existed = self._cache.pop(key, None) is not None

        sidecar = self._sidecar_path(key)
        if sidecar.exists():
            sidecar.unlink()
            existed = True

        pipe = self._pipe_path(key)
        if pipe.exists():
            pipe.unlink()

        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for k in self._cache:
            if k.startswith(manifest_id):
                refs.append(
                    StoredChunkRef(
                        adapter_name="named-pipe",
                        location=f"pipe://{self._dir}/{k}",
                    )
                )
        if self._dir.exists():
            for f in self._dir.iterdir():
                if f.name.startswith(f".{manifest_id}") and f.suffix == ".dat":
                    k = f.name[1:-4]
                    loc = f"pipe://{self._dir}/{k}"
                    if not any(r.location == loc for r in refs):
                        refs.append(StoredChunkRef(adapter_name="named-pipe", location=loc))
        return refs

    async def health_check(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return self._dir.is_dir()
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
