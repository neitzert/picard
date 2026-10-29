"""Plist file adapter -- stores chunks in binary plist files via stdlib plistlib.

Each write_chunk creates a binary plist file with two keys:
  picard_token  -- the UUID token
  picard_chunk  -- the encoded chunk string

No external dependencies -- stdlib only (plistlib, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import plistlib
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "plist_file"


class PlistFileAdapter(Adapter):
    """Scatter chunks in binary plist files."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_plist_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "binary-plist"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.plist")
            data = {
                "picard_token": token,
                "picard_chunk": encoded_chunk,
            }
            with open(path, "wb") as f:
                plistlib.dump(data, f, fmt=plistlib.FMT_BINARY)
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
            with open(loc["path"], "rb") as f:
                data = plistlib.load(f)
            return data["picard_chunk"]

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
