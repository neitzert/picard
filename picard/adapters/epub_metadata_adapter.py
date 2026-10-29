"""EPUB metadata adapter -- stores chunks in <dc:description> of an EPUB OPF.

Each write_chunk creates a minimal valid EPUB (zip with mimetype, container.xml,
and content.opf) where the encoded chunk is stored in <dc:description>.

No external dependencies -- stdlib only (zipfile, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
import zipfile
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "epub_metadata"

_MIMETYPE = "application/epub+zip"
_CONTAINER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _make_opf(description: str) -> str:
    safe = description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf"
         unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">picard-{uuid.uuid4().hex[:8]}</dc:identifier>
    <dc:title>PICARD chunk</dc:title>
    <dc:description>{safe}</dc:description>
    <meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"/>
</package>
"""


def _parse_opf(raw: str) -> str:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    el = root.find(".//{http://purl.org/dc/elements/1.1/}description")
    if el is None or not el.text:
        raise KeyError("no dc:description in OPF")
    return el.text


class EpubMetadataAdapter(Adapter):
    """Scatter chunks in EPUB dc:description metadata."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_epub_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "epub-dc-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.epub")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
                # mimetype MUST be first and uncompressed
                zf.writestr("mimetype", _MIMETYPE)
                zf.writestr("META-INF/container.xml", _CONTAINER_XML)
                zf.writestr("OEBPS/content.opf", _make_opf(encoded_chunk))
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
            with zipfile.ZipFile(loc["path"], "r") as zf:
                opf = zf.read("OEBPS/content.opf").decode("utf-8")
            return _parse_opf(opf)

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
