"""Steganography adapter — stores chunks in image pixel LSBs.

Each chunk is embedded into the least significant bits of a carrier
image's pixel data and saved as a PNG file.  Retrieval extracts the
LSBs back into the original chunk string.

No external image library required — carrier images are abstracted
behind mockable _load_image / _save_image methods.
"""

from __future__ import annotations

import asyncio
import os
import struct
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class SteganographyAdapter(Adapter):
    """Store chunks inside image pixel LSBs."""

    def __init__(
        self,
        image_dir: str,
        *,
        carrier_image: str | None = None,
        max_chunk_bytes: int = 10_000,
    ) -> None:
        self._image_dir = Path(image_dir)
        self._carrier_image = carrier_image
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="steganography",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    # -- mockable I/O -------------------------------------------------

    def _load_image(self, path: str) -> tuple[bytes, int, int]:
        """Load raw pixel bytes, width, height from an image file."""
        with open(path, "rb") as f:
            return f.read(), 256, 256

    def _save_image(self, path: str, pixel_bytes: bytes, width: int, height: int) -> None:
        """Save raw pixel bytes as a PNG."""
        with open(path, "wb") as f:
            f.write(pixel_bytes)

    def _carrier_pixels(self) -> tuple[bytes, int, int]:
        """Return pixel data for the carrier image."""
        if self._carrier_image:
            return self._load_image(self._carrier_image)
        width, height = 256, 256
        return bytes(width * height * 3), width, height

    # -- LSB operations -----------------------------------------------

    @staticmethod
    def _embed_data(pixel_bytes: bytes, data: bytes) -> bytes:
        """Embed *data* into the LSBs of *pixel_bytes*."""
        payload = struct.pack(">I", len(data)) + data
        bits_needed = len(payload) * 8
        if bits_needed > len(pixel_bytes):
            raise ValueError("data too large for carrier image")
        pixels = bytearray(pixel_bytes)
        for i, bit_pos in enumerate(range(bits_needed)):
            byte_idx = bit_pos // 8
            bit_idx = 7 - (bit_pos % 8)
            bit = (payload[byte_idx] >> bit_idx) & 1
            pixels[i] = (pixels[i] & 0xFE) | bit
        return bytes(pixels)

    @staticmethod
    def _extract_data(pixel_bytes: bytes) -> bytes:
        """Extract embedded data from the LSBs of *pixel_bytes*."""
        header_bits = 32
        if len(pixel_bytes) < header_bits:
            raise ValueError("image too small to contain embedded data")
        header_bytes = bytearray(4)
        for i in range(header_bits):
            bit = pixel_bytes[i] & 1
            byte_idx = i // 8
            bit_idx = 7 - (i % 8)
            header_bytes[byte_idx] |= bit << bit_idx
        length = struct.unpack(">I", header_bytes)[0]
        max_length = len(pixel_bytes) // 8 - 4
        if length > max_length:
            raise ValueError(
                f"embedded length {length} exceeds capacity {max_length}"
            )
        total_bits = (4 + length) * 8
        all_bytes = bytearray(4 + length)
        for i in range(total_bits):
            bit = pixel_bytes[i] & 1
            byte_idx = i // 8
            bit_idx = 7 - (i % 8)
            all_bytes[byte_idx] |= bit << bit_idx
        return bytes(all_bytes[4:])

    # -- adapter interface --------------------------------------------

    def _check_path(self, p: Path) -> None:
        if not p.resolve().is_relative_to(self._image_dir.resolve()):
            raise ValueError(f"path escapes image directory: {p}")

    def _filename(self, chunk: Chunk) -> str:
        return f"pchk_{chunk.manifest_id}_b{chunk.block_index}_c{chunk.chunk_index}.png"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        filename = self._filename(chunk)
        data = encoded_chunk.encode("utf-8")

        def _write():
            pixels, w, h = self._carrier_pixels()
            embedded = self._embed_data(pixels, data)
            out_path = str(self._image_dir / filename)
            self._save_image(out_path, embedded, w, h)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="steganography", location=filename)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        def _read():
            p = self._image_dir / ref.location
            self._check_path(p)
            pixels, _, _ = self._load_image(str(p))
            return self._extract_data(pixels).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        def _delete():
            try:
                p = self._image_dir / ref.location
                self._check_path(p)
                p.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"pchk_{manifest_id}_"

        def _list():
            refs: list[StoredChunkRef] = []
            if not self._image_dir.is_dir():
                return refs
            for f in self._image_dir.iterdir():
                if f.name.startswith(prefix) and f.name.endswith(".png"):
                    refs.append(
                        StoredChunkRef(adapter_name="steganography", location=f.name)
                    )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            return self._image_dir.is_dir() and os.access(self._image_dir, os.W_OK)
        except Exception:
            return False
