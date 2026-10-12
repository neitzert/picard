"""CSS steganography adapter — stores chunks in CSS custom properties.

Encodes chunk data as base64 strings in CSS custom properties:
  :root { --picard-0: "base64data..."; }

The custom properties are valid CSS and ignored by browsers.
"""

from __future__ import annotations

import asyncio
import base64
import re
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_PROP_RE = re.compile(r'--picard-data:\s*"([^"]*)"')


class CssStegoAdapter(Adapter):
    """Scatter chunks into CSS custom property values."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_chunk_bytes: int = 32_768,
    ) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="css-stego",
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
        file_path = self._dir / f"{key}.css"
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        css = (
            "/* Generated stylesheet */\n"
            ":root {\n"
            f'  --picard-data: "{b64}";\n'
            "  --font-size: 16px;\n"
            "  --line-height: 1.5;\n"
            "}\n\n"
            "body {\n"
            "  font-size: var(--font-size);\n"
            "  line-height: var(--line-height);\n"
            "}\n"
        )

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(css, encoding="utf-8")

        await asyncio.to_thread(_write)
        location = f"css://{self._dir}/{key}.css"
        return StoredChunkRef(adapter_name="css-stego", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        filename = ref.location.split("://", 1)[1]
        file_path = Path(filename)

        def _read():
            content = file_path.read_text(encoding="utf-8")
            match = _PROP_RE.search(content)
            if not match:
                raise KeyError(f"no picard data in {file_path}")
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
        for f in self._dir.glob("*.css"):
            if f.stem.startswith(manifest_id):
                loc = f"css://{f}"
                refs.append(
                    StoredChunkRef(adapter_name="css-stego", location=loc)
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
        files = list(self._dir.glob("*.css"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(files),
        }
