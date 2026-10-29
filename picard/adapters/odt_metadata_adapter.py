"""ODT metadata adapter -- stores chunks in <dc:description> of ODT meta.xml.

Each write_chunk creates a minimal valid ODT file (zip) with the encoded chunk
stored in the dc:description field of meta.xml.

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

_NAME = "odt_metadata"

_MIMETYPE = "application/vnd.oasis.opendocument.text"
_MANIFEST_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"
                   manifest:version="1.2">
  <manifest:file-entry manifest:full-path="/" manifest:version="1.2"
    manifest:media-type="application/vnd.oasis.opendocument.text"/>
  <manifest:file-entry manifest:full-path="meta.xml"
    manifest:media-type="text/xml"/>
  <manifest:file-entry manifest:full-path="content.xml"
    manifest:media-type="text/xml"/>
</manifest:manifest>
"""
_CONTENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  office:version="1.2">
  <office:body>
    <office:text/>
  </office:body>
</office:document-content>
"""


def _make_meta_xml(description: str) -> str:
    safe = description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
  xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  office:version="1.2">
  <office:meta>
    <dc:description>{safe}</dc:description>
  </office:meta>
</office:document-meta>
"""


def _parse_meta(raw: str) -> str:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    el = root.find(".//{http://purl.org/dc/elements/1.1/}description")
    if el is None or not el.text:
        raise KeyError("no dc:description in ODT meta.xml")
    return el.text


class OdtMetadataAdapter(Adapter):
    """Scatter chunks in ODT meta.xml dc:description."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_odt_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "odt-dc-description"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.odt")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("mimetype", _MIMETYPE)
                zf.writestr("META-INF/manifest.xml", _MANIFEST_XML)
                zf.writestr("meta.xml", _make_meta_xml(encoded_chunk))
                zf.writestr("content.xml", _CONTENT_XML)
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
                meta = zf.read("meta.xml").decode("utf-8")
            return _parse_meta(meta)

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
