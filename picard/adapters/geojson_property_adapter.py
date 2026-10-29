"""GeoJSON property adapter -- stores chunks in GeoJSON Feature properties.

Each write_chunk creates a .geojson file with a single Feature that has
the encoded chunk stored under "picard_chunk" in its properties object.

No external dependencies -- stdlib only (json, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "geojson_property"


class GeoJsonPropertyAdapter(Adapter):
    """Scatter chunks in GeoJSON Feature properties."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_geojson_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "geojson-feature-properties"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.geojson")
            feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
                "properties": {
                    "picard_token": token,
                    "picard_chunk": encoded_chunk,
                },
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(feature, f)
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
                feature = json.load(f)
            return feature["properties"]["picard_chunk"]

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
