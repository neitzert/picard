"""CSV header adapter -- stores chunks encoded in CSV column header names.

The chunk is split across column header names in the first row of a CSV file.
Each header segment is prefixed to ensure parseable column names. A single
data row of zeros maintains CSV structure.

No external dependencies -- stdlib only (csv).
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "csv_header"
_TOKEN_COL = "_picard_token"
_CHUNK_COLS_PREFIX = "_pc"


def _encode_to_headers(token: str, encoded_chunk: str) -> list[str]:
    headers = [_TOKEN_COL + "_" + token]
    data = encoded_chunk
    i = 0
    while data:
        seg = data[:50]
        data = data[50:]
        headers.append(f"{_CHUNK_COLS_PREFIX}{i}_{seg}")
        i += 1
    return headers


def _decode_from_headers(headers: list[str]) -> tuple[str, str]:
    token = ""
    parts = []
    for h in headers:
        if h.startswith(_TOKEN_COL + "_"):
            token = h[len(_TOKEN_COL) + 1:]
        elif h.startswith(_CHUNK_COLS_PREFIX):
            rest = h[len(_CHUNK_COLS_PREFIX):]
            idx_end = rest.index("_")
            parts.append((int(rest[:idx_end]), rest[idx_end + 1:]))
    parts.sort()
    chunk = "".join(p for _, p in parts)
    return token, chunk


class CsvHeaderAdapter(Adapter):
    """Scatter chunks encoded in CSV column header names."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_csv_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "csv-column-header-names"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.csv")
            headers = _encode_to_headers(token, encoded_chunk)
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerow(["0"] * len(headers))
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
            with open(loc["path"], "r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                headers = next(reader)
            _, chunk = _decode_from_headers(headers)
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
