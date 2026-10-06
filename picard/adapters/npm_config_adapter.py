"""NPM Config adapter — stores chunks in .npmrc custom fields.

Hides data in npm configuration files as custom key-value pairs.
Since .npmrc supports arbitrary keys, entries like ``picard:chunk-abc=...``
go unnoticed by most tooling.

No external dependencies beyond Python stdlib.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NpmConfigAdapter(Adapter):
    """Scatter chunks into .npmrc custom configuration fields."""

    def __init__(
        self,
        *,
        npmrc_path: str = "~/.npmrc",
        prefix: str = "picard",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._npmrc_path = os.path.expanduser(npmrc_path)
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="npm-config",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _key_for_id(self, chunk_id: str) -> str:
        """Return the npmrc key string for a chunk id."""
        return f"{self._prefix}:chunk-{chunk_id}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        key = self._key_for_id(chunk_id)
        line = f"{key}={encoded_chunk}\n"

        path = Path(self._npmrc_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            raise RuntimeError(f"failed to write to {self._npmrc_path}: {exc}") from exc

        location = f"npmrc://{self._npmrc_path}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="npm-config", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Extract chunk_id from location: npmrc://{path}/{chunk_id}
        chunk_id = ref.location.rsplit("/", 1)[-1]
        key = self._key_for_id(chunk_id)

        try:
            with open(self._npmrc_path, encoding="utf-8") as f:
                for raw_line in f:
                    stripped = raw_line.strip()
                    if stripped.startswith(f"{key}="):
                        data = stripped[len(key) + 1:]
                        self._cache[ref.location] = data
                        return data
        except OSError:
            pass

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_id = ref.location.rsplit("/", 1)[-1]
        key = self._key_for_id(chunk_id)

        try:
            path = Path(self._npmrc_path)
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                new_lines = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
                path.write_text("".join(new_lines), encoding="utf-8")
        except OSError:
            pass

        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="npm-config", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            path = Path(self._npmrc_path)
            # If the file exists we can read it; if it doesn't, we can
            # still create it, so that's healthy too.
            if path.exists():
                path.read_text(encoding="utf-8")
            return True
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
