"""HTML comments adapter — stores chunks in HTML comment tags.

Creates HTML files with chunk data hidden in comment tags:
  <!-- PICARD:base64-encoded-chunk-data -->

HTML comment tags are not rendered by browsers.
"""

from __future__ import annotations

import asyncio
import base64
import re
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_COMMENT_RE = re.compile(r"<!-- PICARD:(.*?) -->")


class HtmlCommentsAdapter(Adapter):
    """Scatter chunks into HTML comment tags."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_chunk_bytes: int = 65_536,
    ) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="html-comments",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        from picard.chunker import Chunk

        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        file_path = self._dir / f"{key}.html"
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        html = (
            "<!DOCTYPE html>\n"
            "<html><head><title>Page</title></head>\n"
            "<body>\n"
            "<p>Nothing to see here.</p>\n"
            f"<!-- PICARD:{b64} -->\n"
            "</body></html>\n"
        )

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(html, encoding="utf-8")

        await asyncio.to_thread(_write)
        location = f"htmlc://{self._dir}/{key}.html"
        return StoredChunkRef(adapter_name="html-comments", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        filename = ref.location.split("://", 1)[1]
        file_path = Path(filename)

        def _read():
            content = file_path.read_text(encoding="utf-8")
            match = _COMMENT_RE.search(content)
            if not match:
                raise KeyError(f"no PICARD comment in {file_path}")
            return base64.b64decode(match.group(1)).decode()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        filename = ref.location.split("://", 1)[1]
        file_path = Path(filename)
        try:
            file_path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        if not self._dir.is_dir():
            return refs
        for f in self._dir.glob("*.html"):
            if f.stem.startswith(manifest_id):
                loc = f"htmlc://{f}"
                refs.append(
                    StoredChunkRef(adapter_name="html-comments", location=loc)
                )
        return refs

    async def health_check(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return self._dir.is_dir()
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        if not self._dir.is_dir():
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
        files = list(self._dir.glob("*.html"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(files),
        }
