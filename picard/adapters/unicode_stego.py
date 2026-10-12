"""Unicode steganography adapter — stores data in zero-width characters.

Encodes chunk bytes as sequences of zero-width Unicode characters
interleaved with normal cover text.  Zero-width characters are not
rendered visually in most text display contexts.

Zero-width chars used:
  U+200B (zero-width space)     = 00
  U+200C (zero-width non-joiner) = 01
  U+200D (zero-width joiner)     = 10
  U+FEFF (zero-width no-break space) = 11
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_ZW_CHARS = ["​", "‌", "‍", "﻿"]
_ZW_SET = set(_ZW_CHARS)


def _encode_zwc(data: bytes) -> str:
    """Encode bytes as zero-width character string."""
    result: list[str] = []
    for byte in data:
        for shift in (6, 4, 2, 0):
            result.append(_ZW_CHARS[(byte >> shift) & 0x03])
    return "".join(result)


def _decode_zwc(text: str) -> bytes:
    """Extract bytes from zero-width character string."""
    zw_chars = [c for c in text if c in _ZW_SET]
    result: list[int] = []
    for i in range(0, len(zw_chars) - 3, 4):
        byte = 0
        for j, shift in enumerate((6, 4, 2, 0)):
            byte |= _ZW_CHARS.index(zw_chars[i + j]) << shift
        result.append(byte)
    return bytes(result)


class UnicodeStegoAdapter(Adapter):
    """Scatter chunks into invisible zero-width Unicode characters."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        cover_text: str = "The quick brown fox jumps over the lazy dog.",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._dir = Path(output_dir)
        self._cover = cover_text
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="unicode-stego",
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
        file_path = self._dir / f"{key}.txt"
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        hidden = _encode_zwc(b64.encode())
        stego_text = self._cover[:len(self._cover) // 2] + hidden + self._cover[len(self._cover) // 2:]

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            file_path.write_text(stego_text, encoding="utf-8")

        await asyncio.to_thread(_write)
        location = f"zwc://{self._dir}/{key}.txt"
        return StoredChunkRef(adapter_name="unicode-stego", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        filename = ref.location.split("://", 1)[1]
        file_path = Path(filename)

        def _read():
            text = file_path.read_text(encoding="utf-8")
            raw = _decode_zwc(text)
            return base64.b64decode(raw).decode()

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
        for f in self._dir.glob("*.txt"):
            if f.stem.startswith(manifest_id):
                loc = f"zwc://{f}"
                refs.append(
                    StoredChunkRef(adapter_name="unicode-stego", location=loc)
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
        files = list(self._dir.glob("*.txt"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(files),
        }
