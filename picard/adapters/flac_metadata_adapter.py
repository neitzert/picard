"""FLAC metadata adapter -- stores chunks in VORBIS_COMMENT blocks.

Each write_chunk creates a .flac file with a VORBIS_COMMENT metadata
block containing a PICARD_<token>=<encoded_chunk> field. FLAC's Vorbis
comment block is the standard free-form tag mechanism.

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

_NAME = "flac_metadata"

# Minimal valid FLAC file: fLaC marker + STREAMINFO block (34 bytes)
# STREAMINFO is mandatory and must be first. All fields can be zero/minimal.
_MINIMAL_FLAC = (
    b"fLaC"
    # Metadata block header: last=1 (0x80), type=0 (STREAMINFO), length=34
    + b"\x80\x00\x00\x22"
    # STREAMINFO 34 bytes:
    # min_blocksize(2) + max_blocksize(2) + min_framesize(3) + max_framesize(3)
    # + sample_rate(20 bits) + channels(3 bits) + bits_per_sample(5 bits) + total_samples(36 bits)
    # + md5(16 bytes)
    + b"\x00\x10\x00\x10"          # block sizes: 16
    + b"\x00\x00\x00\x00\x00\x00"  # frame sizes: 0
    + b"\x0a\xc4\x42"              # 44100 Hz (0x0AC44), 2 channels, 16 bits
    + b"\x00\x00\x00\x00\x00"      # total samples high bits + low bytes
    + b"\x00" * 16                 # MD5
)


class FlacMetadataAdapter(Adapter):
    """Scatter chunks as VORBIS_COMMENT tags in FLAC files."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_flac_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "mutagen", "substrate": "flac-vorbis-comment"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            try:
                from mutagen.flac import FLAC
            except ImportError as e:
                raise ImportError("mutagen required: pip install mutagen") from e
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.flac")
            with open(path, "wb") as f:
                f.write(_MINIMAL_FLAC)
            tags = FLAC(path)
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
            from mutagen.flac import FLAC
            tags = FLAC(loc["path"])
            key = f"picard_{token}"
            if key not in tags:
                raise KeyError(f"FLAC tag not found: {key}")
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
            import mutagen.flac  # noqa: F401
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        pass
