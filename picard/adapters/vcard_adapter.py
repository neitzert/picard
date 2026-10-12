"""vCard adapter — stores chunks in vCard contact cards.

Chunks are embedded in the NOTE field of vCard 3.0 entries.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class VCardAdapter(Adapter):
    """Scatter chunks into vCard contact entries."""

    def __init__(
        self,
        *,
        file_path: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._max = max_chunk_bytes
        self._file_path = Path(file_path) if file_path else None
        self._cache: dict[str, str] = {}
        if self._file_path and self._file_path.exists():
            self._load_from_file()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="vcard",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_vcf(self) -> str:
        cards = ""
        for loc, data in self._cache.items():
            uid = loc.rsplit("/", 1)[-1]
            cards += (
                "BEGIN:VCARD\n"
                "VERSION:3.0\n"
                f"FN:PICARD Contact {uid}\n"
                f"UID:{uid}\n"
                f"NOTE:{data}\n"
                "END:VCARD\n"
            )
        return cards

    def _flush(self) -> None:
        if self._file_path is None:
            return
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._build_vcf(), encoding="utf-8")

    def _load_from_file(self) -> None:
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            for match in re.finditer(
                r"BEGIN:VCARD\n.*?UID:(.*?)\n.*?NOTE:(.*?)\nEND:VCARD",
                raw,
                re.DOTALL,
            ):
                uid = match.group(1).strip()
                data = match.group(2).strip()
                loc = f"vcard://{uid}"
                self._cache[loc] = data
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"vcard://{chunk_id}"
        self._cache[location] = encoded_chunk
        self._flush()
        return StoredChunkRef(adapter_name="vcard", location=location)

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
            StoredChunkRef(adapter_name="vcard", location=loc)
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
