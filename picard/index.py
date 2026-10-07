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

_PCIX_MAGIC = b"PCIX\x01"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS manifests (
    manifest_id    TEXT PRIMARY KEY,
    path           TEXT NOT NULL,
    original_size  INTEGER NOT NULL,
    block_size     INTEGER NOT NULL,
    total_blocks   INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    tags           TEXT NOT NULL DEFAULT '{}',
    plaintext_hash TEXT NOT NULL DEFAULT '',
    file_type      TEXT NOT NULL DEFAULT 'regular',
    mode           INTEGER NOT NULL DEFAULT 33188,
    mtime          TEXT NOT NULL DEFAULT '',
    ctime          TEXT NOT NULL DEFAULT '',
    atime          TEXT NOT NULL DEFAULT '',
    uid            INTEGER NOT NULL DEFAULT 0,
    gid            INTEGER NOT NULL DEFAULT 0,
    symlink_target TEXT NOT NULL DEFAULT '',
    nlink          INTEGER NOT NULL DEFAULT 1,
    xattrs         TEXT NOT NULL DEFAULT '{}',
    version        INTEGER NOT NULL DEFAULT 1,
    parent_path    TEXT NOT NULL DEFAULT ''
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
CREATE INDEX IF NOT EXISTS idx_manifests_parent ON manifests(parent_path);
CREATE INDEX IF NOT EXISTS idx_manifests_path_version ON manifests(path, version);
"""

_MIGRATIONS = [
    "ALTER TABLE manifests ADD COLUMN plaintext_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chunks ADD COLUMN digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE manifests ADD COLUMN file_type TEXT NOT NULL DEFAULT 'regular'",
    "ALTER TABLE manifests ADD COLUMN mode INTEGER NOT NULL DEFAULT 33188",
    "ALTER TABLE manifests ADD COLUMN mtime TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE manifests ADD COLUMN ctime TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE manifests ADD COLUMN atime TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE manifests ADD COLUMN uid INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE manifests ADD COLUMN gid INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE manifests ADD COLUMN symlink_target TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE manifests ADD COLUMN nlink INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE manifests ADD COLUMN xattrs TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE manifests ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE manifests ADD COLUMN parent_path TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_manifests_parent ON manifests(parent_path)",
    "CREATE INDEX IF NOT EXISTS idx_manifests_path_version ON manifests(path, version)",
]


class Index:
    """Thin wrapper around a SQLite database for manifest bookkeeping."""

    def __init__(
        self, db_path: str | Path = ":memory:", *, passphrase: str | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._passphrase = passphrase
        self._encrypted = False

        if str(db_path) == ":memory:" or passphrase is None:
            self._conn = sqlite3.connect(self._db_path)
        else:
            p = Path(db_path)
            if p.exists() and p.stat().st_size > 0:
                raw = p.read_bytes()
                if raw[: len(_PCIX_MAGIC)] == _PCIX_MAGIC:
                    from picard.crypto import decrypt
                    sql_bytes = decrypt(raw[len(_PCIX_MAGIC) :], passphrase)
                    self._conn = sqlite3.connect(":memory:")
                    self._conn.executescript(sql_bytes.decode("utf-8"))
                    self._encrypted = True
                else:
                    self._conn = sqlite3.connect(":memory:")
                    file_conn = sqlite3.connect(str(db_path))
                    file_conn.backup(self._conn)
                    file_conn.close()
                    self._encrypted = True
            else:
                self._conn = sqlite3.connect(":memory:")
                self._encrypted = True

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
                "INSERT OR REPLACE INTO manifests "
                "(manifest_id, path, original_size, block_size, total_blocks, "
                "created_at, tags, plaintext_hash, file_type, mode, mtime, ctime, "
                "atime, uid, gid, symlink_target, nlink, xattrs, version, parent_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    m.manifest_id,
                    m.path,
                    m.original_size,
                    m.block_size,
                    m.total_blocks,
                    m.created_at,
                    json.dumps(m.tags),
                    m.plaintext_hash,
                    m.file_type,
                    m.mode,
                    m.mtime,
                    m.ctime,
                    m.atime,
                    m.uid,
                    m.gid,
                    m.symlink_target,
                    m.nlink,
                    json.dumps(m.xattrs),
                    m.version,
                    m.parent_path,
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
        def _col(name: str, default=""):
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

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
            file_type=_col("file_type", "regular"),
            mode=_col("mode", 0o100644),
            mtime=_col("mtime", ""),
            ctime=_col("ctime", ""),
            atime=_col("atime", ""),
            uid=_col("uid", 0),
            gid=_col("gid", 0),
            symlink_target=_col("symlink_target", ""),
            nlink=_col("nlink", 1),
            xattrs=json.loads(_col("xattrs", "{}")),
            version=_col("version", 1),
            parent_path=_col("parent_path", ""),
        )

    def get_next_version(self, path: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) as max_v FROM manifests WHERE path=?", (path,)
        ).fetchone()
        return (row["max_v"] or 0) + 1

    def get_manifest_by_path_version(self, path: str, version: int) -> Manifest | None:
        row = self._conn.execute(
            "SELECT * FROM manifests WHERE path=? AND version=? LIMIT 1",
            (path, version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_manifest(row)

    def list_versions(self, path: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT manifest_id, version, created_at, original_size, file_type "
            "FROM manifests WHERE path=? ORDER BY version",
            (path,),
        ).fetchall()
        return [
            {
                "manifest_id": r["manifest_id"],
                "version": r["version"],
                "created_at": r["created_at"],
                "original_size": r["original_size"],
                "file_type": r["file_type"],
            }
            for r in rows
        ]

    def list_children(self, parent_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT path FROM manifests WHERE parent_path=? ORDER BY path",
            (parent_path,),
        ).fetchall()
        return [r["path"] for r in rows]

    def list_paths_with_prefix(self, prefix: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT path FROM manifests WHERE path LIKE ? ORDER BY path",
            (prefix + "%",),
        ).fetchall()
        return [r["path"] for r in rows]

    def update_manifest_metadata(self, manifest_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "mode", "mtime", "ctime", "atime", "uid", "gid",
            "nlink", "symlink_target", "xattrs", "tags",
        }
        sets = []
        vals = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("xattrs", "tags"):
                v = json.dumps(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        vals.append(manifest_id)
        with self._conn:
            self._conn.execute(
                f"UPDATE manifests SET {', '.join(sets)} WHERE manifest_id=?",
                vals,
            )

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def flush(self) -> None:
        """Write current database state to disk (encrypted if passphrase set)."""
        if self._db_path == ":memory:" or not self._encrypted:
            return
        self._write_encrypted()

    def _write_encrypted(self) -> None:
        from picard.crypto import encrypt
        sql_text = "\n".join(self._conn.iterdump()) + "\n"
        ct = encrypt(sql_text.encode("utf-8"), self._passphrase)
        Path(self._db_path).write_bytes(_PCIX_MAGIC + ct)

    def close(self) -> None:
        if self._encrypted and self._db_path != ":memory:":
            self._write_encrypted()
        self._conn.close()
