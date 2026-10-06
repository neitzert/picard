"""Local index — SQLite-backed manifest and chunk-location store.

This is the *only* state PICARD keeps locally.  If the index is lost,
adapters can be scanned (slowly) to rebuild it via ``list_chunks``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from picard.manifest import (
    BlockRecord,
    ChunkRecord,
    ChunkStatus,
    Manifest,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS manifests (
    manifest_id  TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    original_size INTEGER NOT NULL,
    block_size   INTEGER NOT NULL,
    total_blocks INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '{}',
    plaintext_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS blocks (
    manifest_id  TEXT NOT NULL,
    block_index  INTEGER NOT NULL,
    digest       TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    PRIMARY KEY (manifest_id, block_index),
    FOREIGN KEY (manifest_id) REFERENCES manifests(manifest_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    manifest_id   TEXT NOT NULL,
    block_index   INTEGER NOT NULL,
    chunk_index   INTEGER NOT NULL,
    adapter_name  TEXT NOT NULL,
    location      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    digest        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (manifest_id, block_index, chunk_index),
    FOREIGN KEY (manifest_id, block_index)
        REFERENCES blocks(manifest_id, block_index)
);

CREATE INDEX IF NOT EXISTS idx_manifests_path ON manifests(path);
"""

_MIGRATIONS = [
    "ALTER TABLE manifests ADD COLUMN plaintext_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chunks ADD COLUMN digest TEXT NOT NULL DEFAULT ''",
]


class Index:
    """Thin wrapper around a SQLite database for manifest bookkeeping."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                self._conn.execute(migration)
            except sqlite3.OperationalError:
                pass

    # --- write -------------------------------------------------------------

    def save_manifest(self, m: Manifest) -> None:
        """Insert or replace a full manifest (with blocks and chunks)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO manifests VALUES (?,?,?,?,?,?,?,?)",
                (
                    m.manifest_id,
                    m.path,
                    m.original_size,
                    m.block_size,
                    m.total_blocks,
                    m.created_at,
                    json.dumps(m.tags),
                    m.plaintext_hash,
                ),
            )
            for b in m.blocks:
                self._conn.execute(
                    "INSERT OR REPLACE INTO blocks VALUES (?,?,?,?)",
                    (m.manifest_id, b.block_index, b.digest, b.size_bytes),
                )
                for c in b.chunks:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                        (
                            m.manifest_id,
                            b.block_index,
                            c.chunk_index,
                            c.adapter_name,
                            c.location,
                            c.status.value,
                            c.size_bytes,
                            c.digest,
                        ),
                    )

    def update_chunk_status(
        self,
        manifest_id: str,
        block_index: int,
        chunk_index: int,
        status: ChunkStatus,
        location: str | None = None,
    ) -> None:
        """Update status (and optionally location) of a single chunk."""
        with self._conn:
            if location is not None:
                self._conn.execute(
                    "UPDATE chunks SET status=?, location=? "
                    "WHERE manifest_id=? AND block_index=? AND chunk_index=?",
                    (status.value, location, manifest_id, block_index, chunk_index),
                )
            else:
                self._conn.execute(
                    "UPDATE chunks SET status=? "
                    "WHERE manifest_id=? AND block_index=? AND chunk_index=?",
                    (status.value, manifest_id, block_index, chunk_index),
                )

    # --- read --------------------------------------------------------------

    def get_manifest(self, manifest_id: str) -> Manifest | None:
        """Load a full manifest by ID."""
        row = self._conn.execute(
            "SELECT * FROM manifests WHERE manifest_id=?", (manifest_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_manifest(row)

    def get_manifest_by_path(self, path: str) -> Manifest | None:
        """Load the manifest for a given virtual path."""
        row = self._conn.execute(
            "SELECT * FROM manifests WHERE path=? ORDER BY created_at DESC LIMIT 1",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_manifest(row)

    def list_paths(self) -> list[str]:
        """Return all stored virtual paths."""
        rows = self._conn.execute(
            "SELECT DISTINCT path FROM manifests ORDER BY path"
        ).fetchall()
        return [r["path"] for r in rows]

    # --- delete ------------------------------------------------------------

    def delete_manifest(self, manifest_id: str) -> bool:
        """Remove a manifest and its blocks/chunks from the index."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM chunks WHERE manifest_id=?", (manifest_id,)
            )
            self._conn.execute(
                "DELETE FROM blocks WHERE manifest_id=?", (manifest_id,)
            )
            cur = self._conn.execute(
                "DELETE FROM manifests WHERE manifest_id=?", (manifest_id,)
            )
            return cur.rowcount > 0

    # --- internal ----------------------------------------------------------

    def _row_to_manifest(self, row: sqlite3.Row) -> Manifest:
        mid = row["manifest_id"]
        block_rows = self._conn.execute(
            "SELECT * FROM blocks WHERE manifest_id=? ORDER BY block_index", (mid,)
        ).fetchall()
        blocks: list[BlockRecord] = []
        for br in block_rows:
            chunk_rows = self._conn.execute(
                "SELECT * FROM chunks WHERE manifest_id=? AND block_index=? "
                "ORDER BY chunk_index",
                (mid, br["block_index"]),
            ).fetchall()
            chunks = [
                ChunkRecord(
                    chunk_index=cr["chunk_index"],
                    adapter_name=cr["adapter_name"],
                    location=cr["location"],
                    status=ChunkStatus(cr["status"]),
                    size_bytes=cr["size_bytes"],
                    digest=cr["digest"],
                )
                for cr in chunk_rows
            ]
            blocks.append(
                BlockRecord(
                    block_index=br["block_index"],
                    digest=br["digest"],
                    size_bytes=br["size_bytes"],
                    chunks=chunks,
                )
            )
        return Manifest(
            manifest_id=mid,
            path=row["path"],
            original_size=row["original_size"],
            block_size=row["block_size"],
            total_blocks=row["total_blocks"],
            blocks=blocks,
            created_at=row["created_at"],
            tags=json.loads(row["tags"]),
            plaintext_hash=row["plaintext_hash"],
        )

    def close(self) -> None:
        self._conn.close()
