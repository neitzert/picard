"""EXIF metadata adapter — stores chunks in JPEG image metadata.

Each chunk is stored in the EXIF UserComment tag of a minimal JPEG.
Standard EXIF fields (GPS, camera model) are populated with placeholder
values to produce a structurally valid image file.

Requires: pip install picard[exif]  (Pillow)
"""

from __future__ import annotations

import io
import os
import struct
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_EXIF_USER_COMMENT_TAG = 0x9286
_ASCII_PREFIX = b"ASCII\x00\x00\x00"


class ExifAdapter(Adapter):
    """Scatter chunks into JPEG EXIF UserComment fields."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        image_template: str | Path | None = None,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._template = Path(image_template) if image_template else None
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="exif",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _create_jpeg_with_comment(self, data: str, path: Path) -> None:
        from PIL import Image

        if self._template and self._template.exists():
            img = Image.open(self._template).copy()
        else:
            img = Image.new("RGB", (1, 1), color=(128, 128, 128))

        import piexif

        exif_dict = piexif.load(img.info.get("exif", b"")) if img.info.get("exif") else {
            "0th": {},
            "Exif": {},
            "GPS": {},
            "1st": {},
        }
        user_comment = _ASCII_PREFIX + data.encode("utf-8")
        exif_dict["Exif"][_EXIF_USER_COMMENT_TAG] = user_comment

        exif_bytes = piexif.dump(exif_dict)
        img.save(str(path), "JPEG", exif=exif_bytes, quality=95)

    def _read_comment_from_jpeg(self, path: Path) -> str:
        import piexif

        exif_dict = piexif.load(str(path))
        raw = exif_dict["Exif"].get(_EXIF_USER_COMMENT_TAG, b"")

        if raw.startswith(_ASCII_PREFIX):
            return raw[len(_ASCII_PREFIX) :].decode("utf-8")
        return raw.decode("utf-8", errors="replace")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            file_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            file_id = uuid.uuid4().hex

        path = self._dir / f"{file_id}.jpg"
        self._create_jpeg_with_comment(encoded_chunk, path)

        loc = f"exif://{path}"
        return StoredChunkRef(adapter_name="exif", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = Path(ref.location.removeprefix("exif://"))
        return self._read_comment_from_jpeg(path)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = Path(ref.location.removeprefix("exif://"))
        if path.exists():
            path.unlink()
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for f in self._dir.glob("*.jpg"):
            if manifest_id in f.stem:
                refs.append(
                    StoredChunkRef(adapter_name="exif", location=f"exif://{f}")
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() and os.access(self._dir, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        usage = shutil.disk_usage(self._dir)
        files = list(self._dir.glob("*.jpg"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "chunk_count": len(files),
        }
