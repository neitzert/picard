"""DOCX metadata adapter — stores chunks in Word document custom properties.

Each chunk becomes a .docx with data hidden in custom XML properties:
    docx://{output_dir}/{chunk_id}.docx

Custom properties are a standard OOXML feature. Requires: pip install picard-store[docx]
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class DocxMetadataAdapter(Adapter):
    """Store chunks in DOCX document custom properties."""

    def __init__(self, output_dir: str | Path, *, max_chunk_bytes: int = 65_536) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="docx-metadata",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_id = uuid.uuid4().hex

        path = self._dir / f"{chunk_id}.docx"

        def _write():
            import zipfile
            import xml.etree.ElementTree as ET

            self._dir.mkdir(parents=True, exist_ok=True)

            ct_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/></Types>'
            rels_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/></Relationships>'
            doc_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>.</w:t></w:r></w:p></w:body></w:document>'
            word_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'

            ns = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
            vt_ns = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
            custom_root = ET.Element("Properties", xmlns=ns)
            custom_root.set("xmlns:vt", vt_ns)
            prop = ET.SubElement(custom_root, "property", fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}", pid="2", name="picard_data")
            vt_str = ET.SubElement(prop, f"{{{vt_ns}}}lpwstr")
            vt_str.text = encoded_chunk
            custom_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(custom_root, encoding="unicode")

            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("[Content_Types].xml", ct_xml)
                zf.writestr("_rels/.rels", rels_xml)
                zf.writestr("word/document.xml", doc_xml)
                zf.writestr("word/_rels/document.xml.rels", word_rels)
                zf.writestr("docProps/custom.xml", custom_xml)

        await asyncio.to_thread(_write)
        location = f"docx://{path}"
        return StoredChunkRef(adapter_name="docx-metadata", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = Path(ref.location.split("://", 1)[1])

        def _read():
            import zipfile
            import xml.etree.ElementTree as ET

            with zipfile.ZipFile(path, "r") as zf:
                custom_xml = zf.read("docProps/custom.xml")

            root = ET.fromstring(custom_xml)
            ns = {"cp": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
                  "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"}
            for prop in root.findall("cp:property", ns):
                if prop.get("name") == "picard_data":
                    vt = prop.find("vt:lpwstr", ns)
                    if vt is not None and vt.text:
                        return vt.text
            raise KeyError(f"picard_data not found in {path}")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = Path(ref.location.split("://", 1)[1])
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        if self._dir.is_dir():
            for p in self._dir.glob(f"{manifest_id}_*.docx"):
                refs.append(
                    StoredChunkRef(adapter_name="docx-metadata", location=f"docx://{p}")
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() or not self._dir.exists()

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        if not self._dir.exists():
            return {"used_bytes": 0, "free_bytes": None, "total_bytes": None, "chunk_count": 0}
        usage = shutil.disk_usage(self._dir)
        count = sum(1 for _ in self._dir.glob("*.docx"))
        used = sum(f.stat().st_size for f in self._dir.glob("*.docx"))
        return {"used_bytes": used, "free_bytes": usage.free, "total_bytes": usage.total, "chunk_count": count}
