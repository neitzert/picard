"""ID3 tag adapter -- stores chunks as TXXX frames in ID3 tag files.

Each write_chunk creates a .id3 file in the store directory with one
TXXX frame keyed "PICARD:<token>". Cold reads parse the TXXX frame
directly from the file with no in-process state.

Requires: pip install mutagen
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "id3_tags"


class Id3TagsAdapter(Adapter):
    """Scatter chunks as TXXX frames in ID3 tag files."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_id3_")
        self._reg: dict[str, tuple[str, str]] = {}  # token -> (path, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "mutagen", "substrate": "id3-txxx-frame"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            try:
                from mutagen.id3 import ID3, TXXX
            except ImportError as e:
                raise ImportError("mutagen required: pip install mutagen") from e
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.id3")
            tags = ID3()
            tags.add(TXXX(encoding=3, desc=f"PICARD:{token}", text=[encoded_chunk]))
            tags.save(path)
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
            from mutagen.id3 import ID3
            tags = ID3(loc["path"])
            key = f"TXXX:PICARD:{token}"
            if key not in tags:
                raise KeyError(f"TXXX frame not found: {key}")
            return tags[key].text[0]

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
        try:
            import mutagen.id3  # noqa: F401
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        pass
