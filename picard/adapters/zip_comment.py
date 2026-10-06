"""ZIP comment adapter — stores chunks in ZIP archive comment fields.

ZIP files have an archive-level comment field (up to 65535 bytes)
that is invisible to most tools.  ``unzip -l`` doesn't show it,
file managers ignore it, and antivirus rarely inspects it.
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ZipCommentAdapter(Adapter):
    """Scatter chunks into ZIP archive comment fields."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_chunk_bytes: int = 65_535,
    ) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="zip-comment",
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
        file_path = self._dir / f"{key}.zip"

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("data.txt", "placeholder")
                zf.comment = encoded_chunk.encode("utf-8")
            file_path.write_bytes(buf.getvalue())

        await asyncio.to_thread(_write)
        location = f"zip://{self._dir}/{key}.zip"
        return StoredChunkRef(adapter_name="zip-comment", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        filename = ref.location.split("://", 1)[1]
        file_path = Path(filename)

        def _read():
            with zipfile.ZipFile(file_path, "r") as zf:
                comment = zf.comment
            if not comment:
                raise KeyError(f"no comment in {file_path}")
            return comment.decode("utf-8")

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
        for f in self._dir.glob("*.zip"):
            if f.stem.startswith(manifest_id):
                loc = f"zip://{f}"
                refs.append(
                    StoredChunkRef(adapter_name="zip-comment", location=loc)
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
        files = list(self._dir.glob("*.zip"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(files),
        }
