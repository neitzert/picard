"""Bashrc adapter — stores chunks as shell rc-file comments.

Hides data in .bashrc, .profile, or similar shell startup files.
Each chunk is a comment line:
  # PICARD:{chunk_id}:{encoded_data}

These lines are ignored by the shell but survive across sessions.

Requires: write access to the target rc file.
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class BashrcAdapter(Adapter):
    """Scatter chunks into shell rc-file comments."""

    def __init__(
        self,
        *,
        rc_path: str = "~/.bashrc",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._rc_path = os.path.expanduser(rc_path)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}  # location -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bashrc",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        comment_line = f"# PICARD:{chunk_id}:{encoded_chunk}\n"

        with open(self._rc_path, "a") as f:
            f.write(comment_line)

        location = f"bashrc://{self._rc_path}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="bashrc", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Try to recover from the rc file
        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"# PICARD:{chunk_id}:"

        try:
            with open(self._rc_path, "r") as f:
                for line in f:
                    if line.startswith(marker):
                        return line[len(marker):].rstrip("\n")
        except FileNotFoundError:
            pass

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        marker = f"# PICARD:{chunk_id}:"

        # Remove the line from the rc file
        try:
            with open(self._rc_path, "r") as f:
                lines = f.readlines()
            new_lines = [l for l in lines if not l.startswith(marker)]
            if len(new_lines) < len(lines):
                with open(self._rc_path, "w") as f:
                    f.writelines(new_lines)
        except FileNotFoundError:
            pass

        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for location, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="bashrc", location=location))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            # Check if the rc file's directory exists
            parent = os.path.dirname(self._rc_path)
            return os.path.isdir(parent) if parent else True
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
