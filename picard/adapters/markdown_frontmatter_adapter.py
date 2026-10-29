"""Markdown front matter adapter -- stores chunks in YAML front matter blocks.

Each write_chunk creates a .md file with a YAML front matter block:

    ---
    picard_chunk: <encoded_chunk>
    ---

    # PICARD chunk

Cold reads use a regex to extract the picard_chunk value.

No external dependencies -- stdlib only (re, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "markdown_frontmatter"

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_CHUNK_KEY_RE = re.compile(r"^picard_chunk:\s*(.+)$", re.MULTILINE)


def _make_md(encoded_chunk: str) -> str:
    # Encode as a YAML double-quoted string to handle colons and special chars
    safe = encoded_chunk.replace('"', '\\"')
    return f'---\npicard_chunk: "{safe}"\n---\n\n# PICARD chunk\n'


def _parse_md(raw: str) -> str:
    fm_m = _FRONTMATTER_RE.match(raw)
    if not fm_m:
        raise KeyError("no YAML front matter found")
    fm = fm_m.group(1)
    key_m = _CHUNK_KEY_RE.search(fm)
    if not key_m:
        raise KeyError("no picard_chunk key in front matter")
    value = key_m.group(1).strip()
    # Strip surrounding quotes if present
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('\\"', '"')
    return value


class MarkdownFrontmatterAdapter(Adapter):
    """Scatter chunks in Markdown YAML front matter."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_md_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "markdown-yaml-frontmatter"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_md(encoded_chunk))
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
                return _parse_md(f.read())

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
