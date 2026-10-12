"""Barcode adapter — stores chunks as 2D barcode images (DataMatrix, PDF417, Aztec).

Encodes chunk data into high-density 2D barcodes that look like ordinary
shipping labels or ID cards.  A scanner or camera can read them back.

Requires: pip install picard-store[barcode]  (pylibdmtx, pyzbar, Pillow)
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class BarcodeAdapter(Adapter):
    """Scatter chunks into 2D barcode images."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        barcode_type: str = "datamatrix",
        max_chunk_bytes: int = 2335,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._barcode_type = barcode_type
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="barcode",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from pylibdmtx.pylibdmtx import encode as dm_encode  # noqa: F811
        from PIL import Image  # noqa: F811

        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        def _encode():
            data = encoded_chunk.encode("utf-8")
            encoded = dm_encode(data)
            img = Image.frombytes("RGB", (encoded.width, encoded.height), encoded.pixels)
            path = self._output_dir / f"{chunk_id}.png"
            img.save(str(path))
            return path

        path = await asyncio.to_thread(_encode)
        location = f"barcode://{path}"
        return StoredChunkRef(adapter_name="barcode", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image

        path_str = ref.location.removeprefix("barcode://")
        path = Path(path_str)

        def _decode():
            img = Image.open(str(path))
            results = zbar_decode(img)
            if not results:
                raise ValueError(f"no barcode found in {path}")
            return results[0].data.decode("utf-8")

        return await asyncio.to_thread(_decode)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path_str = ref.location.removeprefix("barcode://")
        path = Path(path_str)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for png in self._output_dir.glob(f"{manifest_id}_*.png"):
            refs.append(
                StoredChunkRef(
                    adapter_name="barcode", location=f"barcode://{png}"
                )
            )
        return refs

    async def health_check(self) -> bool:
        import os

        return self._output_dir.is_dir() and os.access(self._output_dir, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        usage = shutil.disk_usage(self._output_dir)
        pngs = list(self._output_dir.glob("*.png"))
        used = sum(f.stat().st_size for f in pngs)
        return {
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "chunk_count": len(pngs),
        }
