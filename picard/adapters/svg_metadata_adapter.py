"""SVG metadata adapter -- stores chunks as <picard:chunk> elements in <metadata>.

Each write_chunk creates an SVG file with a <metadata> element containing a
<picard:chunk> element that holds the encoded chunk as text content.

No external dependencies -- stdlib only (xml.etree.ElementTree, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
import xml.etree.ElementTree as ET
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "svg_metadata"

_SVG_NS = "http://www.w3.org/2000/svg"
_PICARD_NS = "https://picard.storage/ns"

ET.register_namespace("", _SVG_NS)
ET.register_namespace("picard", _PICARD_NS)


def _make_svg(encoded_chunk: str) -> str:
    """Create a minimal SVG with a picard:chunk element in <metadata>."""
    root = ET.Element(f"{{{_SVG_NS}}}svg", {
        "width": "1",
        "height": "1",
        "viewBox": "0 0 1 1",
    })
    meta = ET.SubElement(root, f"{{{_SVG_NS}}}metadata")
    chunk_el = ET.SubElement(meta, f"{{{_PICARD_NS}}}chunk")
    chunk_el.text = encoded_chunk
    ET.SubElement(root, f"{{{_SVG_NS}}}rect", {
        "width": "1", "height": "1", "fill": "white",
    })
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode")


def _parse_svg(raw: str) -> str:
    """Extract the first picard:chunk text from SVG."""
    root = ET.fromstring(raw)
    for el in root.iter(f"{{{_PICARD_NS}}}chunk"):
        if el.text:
            return el.text
    raise KeyError("no picard:chunk element in SVG")


class SvgMetadataAdapter(Adapter):
    """Scatter chunks in SVG <metadata> picard:chunk elements."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_svg_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "svg-metadata"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_svg(encoded_chunk))
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
            with open(loc["path"], "r", encoding="utf-8") as f:
                return _parse_svg(f.read())

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
