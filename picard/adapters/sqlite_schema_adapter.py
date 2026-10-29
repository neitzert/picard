"""SQLite schema adapter -- encodes chunks in SQLite column names.

Each write_chunk creates a SQLite database file with a single table whose
column name encodes the chunk. The column name is built as:
  c_<base32_encoded_chunk>
(base32 gives only alphanumeric + ABCDEFGHIJKLMNOPQRSTUVWXYZ234567 = valid
SQL identifiers; we lowercase the output and use underscores for padding).

Cold read: attach the db file and read the column name from sqlite_master.

No external dependencies -- stdlib only (sqlite3, base64, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "sqlite_schema"
_PREFIX = "pc_"  # column name prefix to identify PICARD data


def _encode_colname(s: str) -> str:
    """Encode string as a valid SQL identifier via base32."""
    b32 = base64.b32encode(s.encode("utf-8")).decode("ascii").lower()
    return _PREFIX + b32.replace("=", "x")  # '=' is not valid in identifiers


def _decode_colname(col: str) -> str:
    """Decode a _PREFIX-prefixed column name back to the original string."""
    if not col.startswith(_PREFIX):
        raise KeyError(f"column {col!r} is not a PICARD column")
    b32 = col[len(_PREFIX):].upper().replace("X", "=")
    return base64.b32decode(b32).decode("utf-8")


class SqliteSchemaAdapter(Adapter):
    """Scatter chunks encoded in SQLite column names."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_sqls_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=4096,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "sqlite-column-names"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.db")
            col = _encode_colname(encoded_chunk)
            with sqlite3.connect(path) as conn:
                conn.execute(f'CREATE TABLE chunks ("{col}" TEXT)')
                conn.commit()
            return token, path, col

        token, path, col = await asyncio.to_thread(_write)
        self._reg[token] = (path, encoded_chunk)
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "path": path, "col": col}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            with sqlite3.connect(loc["path"]) as conn:
                cur = conn.execute(
                    "SELECT name FROM pragma_table_info('chunks') LIMIT 1"
                )
                row = cur.fetchone()
                if not row:
                    raise KeyError("no columns in chunks table")
                return _decode_colname(row[0])

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
                    loc = self._reg[token]
                    col = _encode_colname(data)
                    refs.append(
                        StoredChunkRef(
                            adapter_name=_NAME,
                            location=json.dumps({"token": token, "path": loc[0], "col": col}),
                        )
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
