"""MP4 atom adapter -- stores chunks as custom ----:com.picard: atoms.

Each write_chunk creates a .m4a file seeded from a minimal valid M4A
skeleton, then adds a custom free-form atom keyed
"----:com.picard:PICARD:<token>" carrying the encoded chunk.

Requires: pip install mutagen
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mp4_atoms"

# Minimal valid M4A: ftyp(28) + free(8) + mdat(8) + moov/mvhd(116)
# mvhd version 0: timescale=44100, duration=0, rate=1.0, volume=1.0
_MINIMAL_M4A = bytes.fromhex(
    # ftyp: size=28 (0x1c), major=M4A, minor=0, compat=[M4A, mp42, isom]
    # 4(size)+4(ftyp)+4(major)+4(minor)+4+4+4(compat) = 28
    "0000001c" "66747970" "4d344120" "00000000" "4d344120" "6d703432" "69736f6d"
    # free: size=8
    "00000008" "66726565"
    # mdat: size=8
    "00000008" "6d646174"
    # moov: size=116 (0x74) = 8+108
    "00000074" "6d6f6f76"
    # mvhd: size=108 (0x6c), version=0, flags=0
    "0000006c" "6d766864" "00000000"
    # ctime=0, mtime=0, timescale=44100, duration=0
    "00000000" "00000000" "0000ac44" "00000000"
    # rate=1.0, volume=1.0, reserved (10 bytes)
    "00010000" "0100" "00000000000000000000"
    # matrix (36 bytes = 3 rows * 12 bytes)
    "000100000000000000000000"
    "000000000001000000000000"
    "000000000000000040000000"
    # pre-defined (24 bytes)
    "000000000000000000000000"
    "000000000000000000000000"
    # next track id
    "00000002"
)


class Mp4AtomsAdapter(Adapter):
    """Scatter chunks as custom MP4 free-form atoms."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_mp4_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "mutagen", "substrate": "mp4-freeform-atom"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            try:
                from mutagen.mp4 import MP4, MP4FreeForm
            except ImportError as e:
                raise ImportError("mutagen required: pip install mutagen") from e
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.m4a")
            with open(path, "wb") as f:
                f.write(_MINIMAL_M4A)
            tags = MP4(path)
            atom_key = f"----:com.picard:PICARD:{token}"
            tags[atom_key] = [MP4FreeForm(encoded_chunk.encode("utf-8"))]
            tags.save(path)
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
            from mutagen.mp4 import MP4
            tags = MP4(loc["path"])
            key = f"----:com.picard:PICARD:{token}"
            if key not in tags:
                raise KeyError(f"MP4 atom not found: {key}")
            return bytes(tags[key][0]).decode("utf-8")

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
            import mutagen.mp4  # noqa: F401
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        pass
