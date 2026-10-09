"""Peer discovery and registry for PICARD network transport."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass


@dataclass(slots=True)
class Peer:
    url: str
    adapters: list[str]
    last_seen: float
    status: str


_PEER_SCHEMA = """\
CREATE TABLE IF NOT EXISTS peers (
    url        TEXT PRIMARY KEY,
    adapters   TEXT NOT NULL DEFAULT '[]',
    last_seen  REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active'
);
"""


class PeerRegistry:
    """Track known PICARD peers."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(_PEER_SCHEMA)

    def add(self, url: str, adapters: list[str] | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO peers (url, adapters, last_seen, status) "
                "VALUES (?, ?, ?, ?)",
                (url, json.dumps(adapters or []), time.time(), "active"),
            )

    def remove(self, url: str) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM peers WHERE url=?", (url,))
            return cur.rowcount > 0

    def list_peers(self) -> list[Peer]:
        rows = self._conn.execute(
            "SELECT * FROM peers ORDER BY last_seen DESC",
        ).fetchall()
        return [
            Peer(
                url=r["url"],
                adapters=json.loads(r["adapters"]),
                last_seen=r["last_seen"],
                status=r["status"],
            )
            for r in rows
        ]

    def get(self, url: str) -> Peer | None:
        row = self._conn.execute(
            "SELECT * FROM peers WHERE url=?", (url,),
        ).fetchone()
        if row is None:
            return None
        return Peer(
            url=row["url"],
            adapters=json.loads(row["adapters"]),
            last_seen=row["last_seen"],
            status=row["status"],
        )

    def update_status(self, url: str, status: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE peers SET status=?, last_seen=? WHERE url=?",
                (status, time.time(), url),
            )

    async def discover_from_peer(
        self, url: str, *, client: "httpx.AsyncClient | None" = None,
    ) -> list[str]:
        import httpx

        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=10)
        try:
            resp = await client.get(f"{url}/transport/v1/adapters")
            resp.raise_for_status()
            data = resp.json()
            adapter_names = [a["name"] for a in data.get("adapters", [])]
            self.add(url, adapter_names)
            return adapter_names
        except Exception:
            self.update_status(url, "unreachable")
            return []
        finally:
            if owns_client:
                await client.aclose()
