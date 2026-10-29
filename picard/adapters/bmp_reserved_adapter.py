"""BMP reserved-gap adapter -- data in the gap between BMP header and pixel data.

BMP files have a gap between the file header (14 bytes) + info header (40 bytes)
and the actual pixel data offset. By setting bfOffBits > 54 we get an arbitrary
gap of (bfOffBits - 54) bytes. We store the encoded chunk there.

No external dependencies -- stdlib only (struct, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "bmp_reserved"

_INFOHEADER_SIZE = 40
_FILEHEADER_SIZE = 14
_STANDARD_OFFSET = _FILEHEADER_SIZE + _INFOHEADER_SIZE  # 54


def _make_bmp(payload: bytes) -> bytes:
    """Create a 1x1 white BMP with payload in the header gap."""
    gap = payload  # data goes right after the standard 54-byte header
    pixel_offset = _STANDARD_OFFSET + len(gap)
    file_size = pixel_offset + 4  # 1 pixel = 4 bytes (BGRA or padded BGR)

    # BITMAPFILEHEADER
    file_header = struct.pack(
        "<2sIHHI",
        b"BM",
        file_size,
        0,  # bfReserved1
        0,  # bfReserved2
        pixel_offset,
    )
    # BITMAPINFOHEADER -- 1x1, 32bpp, uncompressed
    info_header = struct.pack(
        "<IiiHHIIiiII",
        _INFOHEADER_SIZE,
        1,       # biWidth
        1,       # biHeight
        1,       # biPlanes
        32,      # biBitCount
        0,       # biCompression (BI_RGB)
        4,       # biSizeImage
        2835,    # biXPelsPerMeter
        2835,    # biYPelsPerMeter
        0,       # biClrUsed
        0,       # biClrImportant
    )
    pixel_data = b"\xff\xff\xff\xff"  # one white pixel
    return file_header + info_header + gap + pixel_data


def _read_gap(raw: bytes) -> bytes:
    """Extract the gap bytes from a BMP file."""
    if raw[:2] != b"BM":
        raise ValueError("not a BMP file")
    pixel_offset = struct.unpack_from("<I", raw, 10)[0]
    gap_start = _STANDARD_OFFSET
    gap_end = pixel_offset
    if gap_end <= gap_start:
        raise KeyError("no data gap in BMP")
    return raw[gap_start:gap_end]


class BmpReservedAdapter(Adapter):
    """Scatter chunks in BMP header-to-pixel gap."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_bmp_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "bmp-header-gap"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.bmp")
            payload = encoded_chunk.encode("utf-8")
            with open(path, "wb") as f:
                f.write(_make_bmp(payload))
            return token, path

        token, path = await asyncio.to_thread(_write)
        self._reg[token] = (path, encoded_chunk)
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "path": path}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            with open(loc["path"], "rb") as f:
                raw = f.read()
            return _read_gap(raw).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        entry = self._reg.pop(token, None)
        path = entry[0] if entry else loc.get("path")
        if path and os.path.exists(path):
            os.unlink(path)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        refs = []
        for token, (path, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(
                        StoredChunkRef(
                            adapter_name=_NAME,
                            location=json.dumps({"token": token, "path": path}),
                        )
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
