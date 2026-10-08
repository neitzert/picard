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
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
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
            self._save_manifest_raw(m)

    def _save_manifest_raw(self, m: Manifest) -> None:
        """Execute save_manifest SQL without transaction wrapper."""
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
        if m.blocks:
            self._conn.executemany(
                "INSERT OR REPLACE INTO blocks VALUES (?,?,?,?)",
                [
                    (m.manifest_id, b.block_index, b.digest, b.size_bytes)
                    for b in m.blocks
                ],
            )
            chunk_rows = [
                (
                    m.manifest_id,
                    b.block_index,
                    c.chunk_index,
                    c.adapter_name,
                    c.location,
                    c.status.value,
                    c.size_bytes,
                    c.digest,
                )
                for b in m.blocks
                for c in b.chunks
            ]
            if chunk_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                    chunk_rows,
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
            "SELECT * FROM manifests WHERE path=? ORDER BY version DESC LIMIT 1",
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

    def list_file_summaries(self) -> list[dict]:
        """Return lightweight summaries for all latest-version files."""
        rows = self._conn.execute(
            "SELECT m.manifest_id, m.path, m.original_size, m.block_size, "
            "m.total_blocks, m.created_at, m.file_type "
            "FROM manifests m "
            "WHERE m.version = ("
            "  SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path"
            ") ORDER BY m.path"
        ).fetchall()
        if not rows:
            return []
        mids = [r["manifest_id"] for r in rows]
        placeholders = ",".join("?" * len(mids))
        chunk_rows = self._conn.execute(
            f"SELECT manifest_id, COUNT(*) AS cnt, "
            f"GROUP_CONCAT(DISTINCT adapter_name) AS adapters "
            f"FROM chunks WHERE manifest_id IN ({placeholders}) "
            f"GROUP BY manifest_id",
            mids,
        ).fetchall()
        chunk_map: dict[str, tuple[int, list[str]]] = {}
        for cr in chunk_rows:
            adapters = sorted(cr["adapters"].split(",")) if cr["adapters"] else []
            chunk_map[cr["manifest_id"]] = (cr["cnt"], adapters)
        result = []
        for r in rows:
            mid = r["manifest_id"]
            cnt, adapters = chunk_map.get(mid, (0, []))
            result.append({
                "path": r["path"],
                "manifest_id": mid,
                "original_size": r["original_size"],
                "block_size": r["block_size"],
                "total_blocks": r["total_blocks"],
                "total_chunks": cnt,
                "created_at": r["created_at"],
                "adapters": adapters,
            })
        return result

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
        joined = self._conn.execute(
            "SELECT b.block_index, b.digest, b.size_bytes, "
            "c.chunk_index, c.adapter_name, c.location, c.status, "
            "c.size_bytes AS c_size, c.digest AS c_digest "
            "FROM blocks b LEFT JOIN chunks c "
            "ON b.manifest_id = c.manifest_id AND b.block_index = c.block_index "
            "WHERE b.manifest_id=? ORDER BY b.block_index, c.chunk_index",
            (mid,),
        ).fetchall()
        blocks_map: dict[int, BlockRecord] = {}
        for jr in joined:
            bi = jr["block_index"]
            if bi not in blocks_map:
                blocks_map[bi] = BlockRecord(
                    block_index=bi,
                    digest=jr["digest"],
                    size_bytes=jr["size_bytes"],
                )
            if jr["chunk_index"] is not None:
                blocks_map[bi].chunks.append(
                    ChunkRecord(
                        chunk_index=jr["chunk_index"],
                        adapter_name=jr["adapter_name"],
                        location=jr["location"],
                        status=ChunkStatus(jr["status"]),
                        size_bytes=jr["c_size"],
                        digest=jr["c_digest"],
                    )
                )
        blocks = list(blocks_map.values())
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

    def list_children_with_metadata(self, parent_path: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT m.path, m.original_size, m.file_type, m.version, "
            "m.mode, m.mtime "
            "FROM manifests m "
            "WHERE m.parent_path = ? AND m.version = ("
            "  SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path"
            ") ORDER BY m.path",
            (parent_path,),
        ).fetchall()
        return [
            {
                "path": r["path"],
                "name": r["path"].rsplit("/", 1)[-1],
                "type": r["file_type"],
                "size": r["original_size"],
                "version": r["version"],
                "mtime": r["mtime"],
                "mode": r["mode"],
            }
            for r in rows
        ]

    def list_children_rich(self, parent_path: str) -> list[dict]:
        """Return children with path, file_type, and original_size (latest version)."""
        rows = self._conn.execute(
            "SELECT m.path, m.file_type, m.original_size "
            "FROM manifests m WHERE m.parent_path=? "
            "AND m.version = ("
            "  SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path"
            ") ORDER BY m.path",
            (parent_path,),
        ).fetchall()
        return [
            {"path": r["path"], "file_type": r["file_type"],
             "original_size": r["original_size"]}
            for r in rows
        ]

    def tree_query(self, root: str = "/", max_depth: int = 64) -> list[dict]:
        """Return all descendants of *root* up to *max_depth* via recursive CTE."""
        rows = self._conn.execute(
            "WITH RECURSIVE descendants(path, file_type, original_size, parent_path, depth) AS ("
            "  SELECT m.path, m.file_type, m.original_size, m.parent_path, 0 "
            "  FROM manifests m WHERE m.parent_path=? "
            "  AND m.version = (SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path) "
            "  UNION ALL "
            "  SELECT m.path, m.file_type, m.original_size, m.parent_path, d.depth + 1 "
            "  FROM manifests m INNER JOIN descendants d ON m.parent_path = d.path "
            "  WHERE d.file_type = 'directory' AND d.depth < ? "
            "  AND m.version = (SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path) "
            ") SELECT path, file_type, original_size, parent_path, depth FROM descendants "
            "ORDER BY path",
            (root, max_depth),
        ).fetchall()
        return [
            {"path": r["path"], "file_type": r["file_type"],
             "original_size": r["original_size"], "parent_path": r["parent_path"],
             "depth": r["depth"]}
            for r in rows
        ]

    def du_stats(self) -> tuple[dict[str, dict[str, int]], int]:
        """Return per-adapter chunk stats and file count via SQL aggregates.

        Returns (adapter_stats, file_count) where adapter_stats maps
        adapter_name -> {"chunk_count": N, "bytes": N}.
        Only counts chunks belonging to each path's latest version.
        """
        rows = self._conn.execute(
            "SELECT c.adapter_name, COUNT(*) AS cnt, "
            "COALESCE(SUM(c.size_bytes), 0) AS total "
            "FROM chunks c INNER JOIN manifests m "
            "ON c.manifest_id = m.manifest_id "
            "WHERE m.version = ("
            "  SELECT MAX(m2.version) FROM manifests m2 WHERE m2.path = m.path"
            ") GROUP BY c.adapter_name"
        ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for r in rows:
            stats[r["adapter_name"]] = {
                "chunk_count": r["cnt"],
                "bytes": r["total"],
            }
        fc = self._conn.execute(
            "SELECT COUNT(DISTINCT path) AS n FROM manifests"
        ).fetchone()
        return stats, fc["n"]

    def chunk_stats_for_manifest(self, manifest_id: str) -> tuple[dict[str, int], int]:
        """Return (adapter_distribution, total_chunk_bytes) via SQL aggregate."""
        rows = self._conn.execute(
            "SELECT adapter_name, COUNT(*) AS cnt, "
            "COALESCE(SUM(size_bytes), 0) AS total "
            "FROM chunks WHERE manifest_id=? GROUP BY adapter_name",
            (manifest_id,),
        ).fetchall()
        dist: dict[str, int] = {}
        total = 0
        for r in rows:
            dist[r["adapter_name"]] = r["cnt"]
            total += r["total"]
        return dist, total

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
        import tempfile
        from picard.crypto import encrypt
        sql_text = "\n".join(self._conn.iterdump()) + "\n"
        ct = encrypt(sql_text.encode("utf-8"), self._passphrase)
        dest = Path(self._db_path)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
        try:
            with open(fd, "wb") as f:
                f.write(_PCIX_MAGIC + ct)
            import os
            os.replace(tmp, str(dest))
        except BaseException:
            import os
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def close(self) -> None:
        if self._encrypted and self._db_path != ":memory:":
            self._write_encrypted()
        self._conn.close()
