"""GIF comment adapter -- stores chunks in GIF comment extension blocks.

GIF 89a supports comment extensions (0x21 0xFE). Each write_chunk creates
a minimal GIF file containing a comment extension block holding the encoded
chunk followed by 0xFF (no more data blocks) before the GIF terminator.

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

_NAME = "gif_comment"

# GIF header + minimal 1x1 transparent GIF (pre-terminator)
_GIF_HEADER = (
    b"GIF89a"
    b"\x01\x00\x01\x00"  # 1x1 pixels
    b"\x00"               # global color table flag = 0 (no GCT)
    b"\x00\x00"          # background + pixel aspect
    # Image descriptor
    b"\x2c"              # image separator
    b"\x00\x00\x00\x00\x01\x00\x01\x00\x00"  # left,top,width,height,flags
    # Minimal LZW compressed 1-pixel image (code_size=2)
    b"\x02\x02\x4c\x01\x00"  # LZW min code size, block, data, terminator
)
_GIF_TRAILER = b"\x3b"  # GIF file terminator


def _encode_comment(data: bytes) -> bytes:
    """Encode bytes as GIF comment extension sub-blocks (max 255 each)."""
    out = b"\x21\xfe"  # comment extension introducer
    while data:
        chunk = data[:255]
        data = data[255:]
        out += bytes([len(chunk)]) + chunk
    out += b"\x00"  # block terminator
    return out


def _decode_comment(raw: bytes) -> bytes:
    """Parse the first comment extension from raw GIF bytes."""
    i = 0
    while i < len(raw) - 1:
        if raw[i] == 0x21 and raw[i + 1] == 0xFE:
            i += 2
            out = b""
            while raw[i] != 0x00:
                blen = raw[i]
                i += 1
                out += raw[i:i + blen]
                i += blen
            return out
        i += 1
    raise KeyError("no GIF comment extension found")


class GifCommentAdapter(Adapter):
    """Scatter chunks in GIF comment extension blocks."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_gif_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "gif-comment-extension"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.gif")
            comment_block = _encode_comment(encoded_chunk.encode("utf-8"))
            with open(path, "wb") as f:
                f.write(_GIF_HEADER + comment_block + _GIF_TRAILER)
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
            return _decode_comment(raw).decode("utf-8")

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
        return True  # stdlib only

    async def close(self) -> None:
        pass
