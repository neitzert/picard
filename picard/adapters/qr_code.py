"""QR code adapter — stores chunks as QR code PNG images.

Each chunk is encoded into a QR code and saved as a PNG file in a
configured directory.  Retrieval decodes the QR image back to text.

Requires: pip install picard[qr]   (qrcode, Pillow or pyzbar for decode)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class QrCodeAdapter(Adapter):
    """Scatter chunks into QR code PNG images on disk."""

    def __init__(
        self,
        output_dir: str,
        *,
        max_chunk_bytes: int = 2900,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._max = max_chunk_bytes

    def _ensure_dir(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _filename(manifest_id: str, block: int, chunk_idx: int) -> str:
        return f"pchk_{manifest_id}_b{block}_c{chunk_idx}.png"

    def _generate_qr(self, data: str) -> bytes:
        """Generate QR code PNG bytes. Override or mock for testing."""
        import qrcode
        import io

        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _decode_qr(self, image_bytes: bytes) -> str:
        """Decode QR code image bytes to string. Override or mock for testing."""
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))
        results = pyzbar_decode(img)
        if not results:
            raise ValueError("no QR code found in image")
        return results[0].data.decode()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="qr-code",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        filename = self._filename(
            chunk.manifest_id, chunk.block_index, chunk.chunk_index
        )
        self._ensure_dir()
        image_bytes = self._generate_qr(encoded_chunk)
        filepath = self._output_dir / filename

        def _write():
            filepath.write_bytes(image_bytes)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="qr-code", location=filename)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        filepath = self._output_dir / ref.location

        def _read():
            return filepath.read_bytes()

        image_bytes = await asyncio.to_thread(_read)
        return self._decode_qr(image_bytes)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        filepath = self._output_dir / ref.location

        def _delete():
            try:
                filepath.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"pchk_{manifest_id}_"

        def _list():
            if not self._output_dir.exists():
                return []
            return [
                StoredChunkRef(adapter_name="qr-code", location=f.name)
                for f in self._output_dir.iterdir()
                if f.name.startswith(prefix) and f.name.endswith(".png")
            ]

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            self._ensure_dir()
            test_file = self._output_dir / ".picard_health"

            def _check():
                test_file.write_text("ok")
                test_file.unlink()
                return True

            return await asyncio.to_thread(_check)
        except Exception:
            return False
