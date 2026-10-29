"""WebVTT cue note adapter -- stores chunks in WebVTT NOTE blocks.

WebVTT (Web Video Text Tracks) supports NOTE blocks for arbitrary comments.
This adapter creates a .vtt file with a NOTE block containing the token and
chunk. Cold reads parse the NOTE block with stdlib re.

No external dependencies -- stdlib only.
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

_NAME = "vtt_cue_note"


def _make_vtt(token: str, encoded_chunk: str) -> str:
    return (
        "WEBVTT\n\n"
        f"NOTE PICARD_TOKEN:{token}\n"
        f"{encoded_chunk}\n\n"
        "00:00:00.000 --> 00:00:01.000\n"
        ".\n"
    )


def _parse_vtt(text: str) -> tuple[str, str]:
    m = re.search(r"NOTE PICARD_TOKEN:(\S+)\n(.+?)(?:\n\n|\Z)", text, re.DOTALL)
    if not m:
        raise KeyError("PICARD NOTE block not found in VTT")
    token = m.group(1)
    chunk = m.group(2).strip()
    return token, chunk


class VttCueNoteAdapter(Adapter):
    """Scatter chunks in WebVTT NOTE blocks."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_vtt_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "webvtt-note-block"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.vtt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_vtt(token, encoded_chunk))
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
                text = f.read()
            _, chunk = _parse_vtt(text)
            return chunk

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
