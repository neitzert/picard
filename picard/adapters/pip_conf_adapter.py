"""pip.conf adapter — hides chunks in pip configuration extra-index-url entries.

Abuses the pip.conf [global] extra-index-url field by appending fake
package index URLs that encode chunk data in the path component:
  https://picard.local/{chunk_id}/{encoded_data}

These URLs look like legitimate private PyPI mirrors to casual inspection.

Requires: no additional dependencies (stdlib only for file I/O)
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class PipConfAdapter(Adapter):
    """Scatter chunks into pip.conf extra-index-url entries."""

    def __init__(
        self,
        *,
        conf_path: str = "~/.pip/pip.conf",
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._conf_path = os.path.expanduser(conf_path)
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pip-conf",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"pip-conf://{chunk_id}"

        # Build a fake extra-index-url line
        url_line = f"https://picard.local/{chunk_id}/{encoded_chunk}"

        # In a real scenario we would append to pip.conf:
        #   [global]
        #   extra-index-url = https://picard.local/{id}/{data}
        # For safety we only touch _cache; real write would be:
        #   self._append_to_conf(url_line)

        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="pip-conf", location=location)

    def _append_to_conf(self, url_line: str) -> None:  # pragma: no cover
        """Append an extra-index-url entry to pip.conf (real implementation)."""
        os.makedirs(os.path.dirname(self._conf_path), exist_ok=True)

        lines: list[str] = []
        if os.path.exists(self._conf_path):
            with open(self._conf_path, "r") as f:
                lines = f.readlines()

        # Find or create [global] section
        global_idx = -1
        for i, line in enumerate(lines):
            if line.strip().lower() == "[global]":
                global_idx = i
                break

        entry = f"extra-index-url = {url_line}\n"
        if global_idx == -1:
            lines.append("[global]\n")
            lines.append(entry)
        else:
            lines.insert(global_idx + 1, entry)

        with open(self._conf_path, "w") as f:
            f.writelines(lines)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        # Fallback: parse pip.conf for the chunk_id marker
        chunk_id = ref.location.split("://", 1)[-1]
        marker = f"picard.local/{chunk_id}/"

        if os.path.exists(self._conf_path):
            with open(self._conf_path, "r") as f:
                for line in f:
                    if marker in line:
                        # Extract data after the marker
                        idx = line.index(marker) + len(marker)
                        return line[idx:].strip()

        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._cache
        self._cache.pop(ref.location, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for loc, data in self._cache.items():
            try:
                chunk = Chunk.decode(data)
                if chunk.manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="pip-conf", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            parent = os.path.dirname(self._conf_path)
            return os.path.isdir(parent) or True
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
