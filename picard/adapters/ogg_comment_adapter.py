"""OGG Vorbis comment adapter -- stores chunks in Vorbis comment fields.

Each write_chunk creates an .ogg file and inserts a PICARD_TOKEN comment
field carrying the encoded chunk. OGG Vorbis comment fields are arbitrary
key=value pairs in the file's second packet.

Requires: pip install mutagen
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

_NAME = "ogg_comment"

# Minimal OGG Vorbis file: identification packet + comment packet + setup packet
# Enough for mutagen to open and modify the comment header.
# Built from first principles: three OGG pages (ID, comment, setup).
def _make_minimal_ogg() -> bytes:
    import struct, zlib

    def ogg_page(serial: int, seq: int, granule: int, flags: int, packets: list[bytes]) -> bytes:
        # Lace the packets into segments (max 255 bytes each)
        segments = []
        for pkt in packets:
            remaining = pkt
            while len(remaining) > 255:
                segments.append(255)
                remaining = remaining[255:]
            segments.append(len(remaining))
        # OGG page header: OggS + version + htype + granule + serial + seq + crc + nseg
        # CRC is 0 initially, computed over entire page with CRC bytes zeroed
        header = struct.pack(
            "<4sBBqIIIB",
            b"OggS", 0, flags, granule, serial, seq, 0, len(segments)
        )
        header += bytes(segments)
        body = b"".join(packets)
        # CRC32 over page with CRC field zeroed; CRC field is at bytes 22-25
        page = header + body
        crc = zlib.crc32(page) & 0xFFFFFFFF
        # Patch CRC at offset 22
        return page[:22] + struct.pack("<I", crc) + page[26:]

    serial = 12345

    # Identification packet (7 bytes header + vorbis ident data)
    ident = (
        b"\x01vorbis"
        + struct.pack("<IBIIIIBB", 0, 1, 44100, 0, 0, 0, 0xb8, 1)  # version, channels, rate, bitr_max, bitr_nom, bitr_min, blocksizes, framing
    )
    # OGG page 0: identification (bos flag=0x02)
    page0 = ogg_page(serial, 0, 0, 0x02, [ident])

    # Comment packet
    vendor = b"picard"
    comment_pkt = (
        b"\x03vorbis"
        + struct.pack("<I", len(vendor)) + vendor
        + struct.pack("<I", 0)  # zero user comments
        + b"\x01"               # framing bit
    )
    page1 = ogg_page(serial, 1, 0, 0x00, [comment_pkt])

    # Setup packet (minimal: just enough for mutagen to accept)
    # A real setup packet is complex; use a stub that mutagen tolerates
    setup_pkt = b"\x05vorbis" + b"\x00" * 16 + b"\x01"
    page2 = ogg_page(serial, 2, 0, 0x00, [setup_pkt])

    return page0 + page1 + page2


class OggCommentAdapter(Adapter):
    """Scatter chunks as Vorbis comment fields in OGG files."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_ogg_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "mutagen", "substrate": "ogg-vorbis-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            try:
                from mutagen.oggvorbis import OggVorbis
            except ImportError as e:
                raise ImportError("mutagen required: pip install mutagen") from e
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.ogg")
            with open(path, "wb") as f:
                f.write(_make_minimal_ogg())
            tags = OggVorbis(path)
            tags[f"picard_{token}"] = [encoded_chunk]
            tags.save()
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
            from mutagen.oggvorbis import OggVorbis
            tags = OggVorbis(loc["path"])
            key = f"picard_{token}"
            if key not in tags:
                raise KeyError(f"Vorbis comment not found: {key}")
            return tags[key][0]

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
        try:
            import mutagen.oggvorbis  # noqa: F401
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        pass
