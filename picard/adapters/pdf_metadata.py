"""PDF metadata adapter — stores chunks in PDF document info dictionaries.

Creates minimal valid PDFs with chunk data embedded in the /Info dictionary
(Creator, Subject, Keywords) or XMP metadata stream.  Looks like an
ordinary PDF to anyone who opens it.

Requires: pathlib (stdlib)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class PdfMetadataAdapter(Adapter):
    """Scatter chunks into PDF document metadata."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pdf-metadata",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _generate_pdf(self, data: str) -> bytes:
        escaped = data.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        objects = []
        objects.append("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
        objects.append(
            "2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        )
        objects.append(
            "3 0 obj\n<< /Type /Page /Parent 2 0 R "
            "/MediaBox [0 0 612 792] >>\nendobj\n"
        )
        objects.append(
            f"4 0 obj\n<< /Creator (picard) /Subject ({escaped}) >>\nendobj\n"
        )
        body = ""
        offsets = []
        header = "%PDF-1.4\n"
        pos = len(header)
        for obj in objects:
            offsets.append(pos)
            body += obj
            pos += len(obj)
        xref = "xref\n"
        xref += f"0 {len(objects) + 1}\n"
        xref += "0000000000 65535 f \n"
        for off in offsets:
            xref += f"{off:010d} 00000 n \n"
        xref += "trailer\n"
        xref += f"<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\n"
        xref += "startxref\n"
        xref += f"{pos}\n"
        xref += "%%EOF\n"
        return (header + body + xref).encode("latin-1")

    def _extract_subject(self, pdf_bytes: bytes) -> str:
        text = pdf_bytes.decode("latin-1")
        marker = "/Subject ("
        start = text.find(marker)
        if start < 0:
            raise ValueError("no Subject field in PDF")
        start += len(marker)
        depth = 1
        end = start
        while end < len(text) and depth > 0:
            ch = text[end]
            if ch == "\\" :
                end += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        raw = text[start:end]
        return raw.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import asyncio

        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        pdf_bytes = self._generate_pdf(encoded_chunk)
        path = self._output_dir / f"{chunk_id}.pdf"

        await asyncio.to_thread(path.write_bytes, pdf_bytes)

        location = f"pdf://{path}"
        return StoredChunkRef(adapter_name="pdf-metadata", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import asyncio

        path_str = ref.location.removeprefix("pdf://")
        path = Path(path_str)

        pdf_bytes = await asyncio.to_thread(path.read_bytes)
        return self._extract_subject(pdf_bytes)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path_str = ref.location.removeprefix("pdf://")
        path = Path(path_str)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for pdf in self._output_dir.glob(f"{manifest_id}_*.pdf"):
            refs.append(
                StoredChunkRef(
                    adapter_name="pdf-metadata", location=f"pdf://{pdf}"
                )
            )
        return refs

    async def health_check(self) -> bool:
        import os

        return self._output_dir.is_dir() and os.access(self._output_dir, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        usage = shutil.disk_usage(self._output_dir)
        pdfs = list(self._output_dir.glob("*.pdf"))
        used = sum(f.stat().st_size for f in pdfs)
        return {
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "chunk_count": len(pdfs),
        }
