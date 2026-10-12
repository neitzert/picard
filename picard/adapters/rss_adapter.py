"""RSS adapter — stores chunks as RSS feed items.

Chunks are injected as <item> elements in an RSS 2.0 feed,
with the encoded data in the <description> element.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class RSSAdapter(Adapter):
    """Scatter chunks into RSS feed items."""

    def __init__(
        self,
        feed_title: str = "PICARD Feed",
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
            name="rss",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_feed(self) -> str:
        items = ""
        for loc, data in self._cache.items():
            guid = loc.rsplit("/", 1)[-1]
            items += (
                f"  <item>\n"
                f"    <title>PICARD:{guid}</title>\n"
                f"    <guid>{guid}</guid>\n"
                f"    <description><![CDATA[{data}]]></description>\n"
                f"  </item>\n"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0">\n'
            f"<channel>\n"
            f"  <title>{self._feed_title}</title>\n"
            f"{items}"
            f"</channel>\n"
            f"</rss>\n"
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
                r"<guid>(.*?)</guid>\s*<description><!\[CDATA\[(.*?)\]\]></description>",
                raw,
                re.DOTALL,
            ):
                guid = match.group(1)
                data = match.group(2)
                loc = f"rss://{self._feed_title}/{guid}"
                self._cache[loc] = data
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"rss://{self._feed_title}/{chunk_id}"
        self._cache[location] = encoded_chunk
        self._flush()
        return StoredChunkRef(adapter_name="rss", location=location)

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
            StoredChunkRef(adapter_name="rss", location=loc)
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
