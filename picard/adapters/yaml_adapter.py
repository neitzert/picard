"""YAML adapter — stores chunks in a YAML document.

Each chunk becomes a mapping entry keyed by its UUID.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class YAMLAdapter(Adapter):
    """Scatter chunks into YAML document entries."""

    def __init__(
        self,
        *,
        file_path: str | None = None,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._max = max_chunk_bytes
        self._file_path = Path(file_path) if file_path else None
        self._cache: dict[str, str] = {}
        if self._file_path and self._file_path.exists():
            self._load_from_file()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="yaml",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_yaml(self) -> str:
        lines = ["picard:\n", "  chunks:\n"]
        for loc, data in self._cache.items():
            key = loc.rsplit("/", 1)[-1]
            escaped = data.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'    {key}: "{escaped}"\n')
        return "".join(lines)

    def _flush(self) -> None:
        if self._file_path is None:
            return
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._build_yaml(), encoding="utf-8")

    def _load_from_file(self) -> None:
        try:
            import yaml

            raw = self._file_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            chunks = data.get("picard", {}).get("chunks", {})
            for key, value in chunks.items():
                loc = f"yaml://chunks/{key}"
                self._cache[loc] = str(value)
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"yaml://chunks/{chunk_id}"
        self._cache[location] = encoded_chunk
        self._flush()
        return StoredChunkRef(adapter_name="yaml", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        if self._file_path and self._file_path.exists():
            self._load_from_file()
            if ref.location in self._cache:
                return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = self._cache.pop(ref.location, None) is not None
        if existed:
            self._flush()
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="yaml", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        if self._file_path:
            return self._file_path.parent.exists()
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
