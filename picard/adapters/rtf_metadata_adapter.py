"""RTF metadata adapter -- stores chunks in the RTF \\doccomm field.

Each write_chunk creates a minimal RTF file with an \\info group containing
\\doccomm {<encoded_chunk>}. Cold reads parse the RTF with a regex.

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

_NAME = "rtf_metadata"

# Regex to extract the content of \doccomm {...}
_DOCCOMM_RE = re.compile(r"\{\\doccomm ([^}]*)\}", re.DOTALL)


def _make_rtf(encoded_chunk: str) -> str:
    # RTF special chars: \ { } must be escaped
    safe = encoded_chunk.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return (
        "{\\rtf1\\ansi\\deff0\n"
        "{\\info\n"
        "{\\doccomm " + safe + "}\n"
        "}\n"
        "{\\*\\generator PICARD}\n"
        "}\n"
    )


def _parse_rtf(raw: str) -> str:
    m = _DOCCOMM_RE.search(raw)
    if not m:
        raise KeyError("no \\doccomm found in RTF")
    text = m.group(1)
    # Unescape RTF special chars
    text = text.replace("\\\\", "\x00BSLASH\x00")
    text = text.replace("\\{", "{").replace("\\}", "}")
    text = text.replace("\x00BSLASH\x00", "\\")
    return text


class RtfMetadataAdapter(Adapter):
    """Scatter chunks in RTF \\doccomm metadata field."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_rtf_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "rtf-doccomm"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.rtf")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_rtf(encoded_chunk))
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
                return _parse_rtf(f.read())

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
