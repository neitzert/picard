"""Atom adapter — stores chunks as Atom feed entries.

Chunks are injected as <entry> elements in an Atom 1.0 feed,
with the encoded data in the <content> element.

Pure in-memory / file-based — no network calls needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class AtomAdapter(Adapter):
    """Scatter chunks into Atom feed entries."""

    def __init__(
        self,
        feed_title: str = "PICARD Atom Feed",
        *,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._feed_title = feed_title
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="atom",
            max_chunk_bytes=self._max,
            persistent=False,
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

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"atom://{self._feed_title}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="atom", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="atom", location=loc)
            for loc in self._cache
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
