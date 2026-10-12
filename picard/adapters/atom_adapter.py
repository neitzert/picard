"""Atom adapter — stores chunks as Atom feed entries.

Chunks are injected as <entry> elements in an Atom 1.0 feed,
with the encoded data in the <content> element.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class AtomAdapter(Adapter):
    """Scatter chunks into Atom feed entries."""

    def __init__(
        self,
        feed_title: str = "PICARD Atom Feed",
        *,
        file_path: str | None = None,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._feed_title = feed_title
        self._max = max_chunk_bytes
        self._file_path = Path(file_path) if file_path else None
        self._cache: dict[str, str] = {}
        if self._file_path and self._file_path.exists():
            self._load_from_file()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="atom",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_feed(self) -> str:
        now = datetime.now(timezone.utc).isoformat()
        entries = ""
        for loc, data in self._cache.items():
            entry_id = loc.rsplit("/", 1)[-1]
            entries += (
                f"  <entry>\n"
                f"    <title>PICARD:{entry_id}</title>\n"
                f"    <id>urn:picard:{entry_id}</id>\n"
                f"    <updated>{now}</updated>\n"
                f"    <content type=\"text\">{data}</content>\n"
                f"  </entry>\n"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<feed xmlns="http://www.w3.org/2005/Atom">\n'
            f"  <title>{self._feed_title}</title>\n"
            f"  <updated>{now}</updated>\n"
            f"{entries}"
            f"</feed>\n"
        )

    def _flush(self) -> None:
        if self._file_path is None:
            return
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._build_feed(), encoding="utf-8")

    def _load_from_file(self) -> None:
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            for match in re.finditer(
                r"<id>urn:picard:(.*?)</id>.*?<content[^>]*>(.*?)</content>",
                raw,
                re.DOTALL,
            ):
                entry_id = match.group(1)
                data = match.group(2)
                loc = f"atom://{self._feed_title}/{entry_id}"
                self._cache[loc] = data
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"atom://{self._feed_title}/{chunk_id}"
        self._cache[location] = encoded_chunk
        self._flush()
        return StoredChunkRef(adapter_name="atom", location=location)

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
            StoredChunkRef(adapter_name="atom", location=loc)
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
