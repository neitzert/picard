"""JSON comment adapter -- stores chunks in JSONC-style comment lines.

Stores the chunk as a // comment line before a minimal JSON object.
JSONC (JSON with comments) is used by VS Code, TypeScript tsconfig, etc.
The file is NOT valid JSON but parses fine with JSONC-aware readers.

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

_NAME = "json_comment"


def _make_jsonc(token: str, encoded_chunk: str) -> str:
    return (
        f"// PICARD_TOKEN: {token}\n"
        f"// PICARD_CHUNK: {encoded_chunk}\n"
        "{\n"
        f'  "picard_token": "{token}"\n'
        "}\n"
    )


def _parse_jsonc(text: str) -> tuple[str, str]:
    token_m = re.search(r"^// PICARD_TOKEN: (\S+)", text, re.MULTILINE)
    chunk_m = re.search(r"^// PICARD_CHUNK: (.+)$", text, re.MULTILINE)
    if not token_m or not chunk_m:
        raise KeyError("PICARD fields not found in JSONC comments")
    return token_m.group(1), chunk_m.group(1).strip()


class JsonCommentAdapter(Adapter):
    """Scatter chunks as JSONC // comment lines."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_jsonc_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "jsonc-comment-lines"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.jsonc")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_jsonc(token, encoded_chunk))
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
            _, chunk = _parse_jsonc(text)
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
