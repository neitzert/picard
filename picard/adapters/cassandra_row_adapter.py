"""Cassandra row adapter -- stores chunks in a Cassandra table via asyncio.

Uses the cassandra-driver with asyncio integration. Table schema uses a
composite primary key (manifest_id, token) for efficient list_chunks queries.
Distinct from cassandra_adapter.py which uses a (uuid) primary key schema.

Requires: pip install cassandra-driver
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "cassandra-row"
_KEYSPACE = "picard_kv"
_TABLE = "chunks_v2"

_DDL_KEYSPACE = (
    f"CREATE KEYSPACE IF NOT EXISTS {_KEYSPACE} "
    "WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}"
)
_DDL_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {_KEYSPACE}.{_TABLE} ("
    "manifest_id text, token text, chunk text, "
    "PRIMARY KEY (manifest_id, token)"
    ")"
)


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class CassandraRowAdapter(Adapter):

    def __init__(
        self,
        *,
        contact_points: list[str] | None = None,
        port: int = 9042,
        username: str = "",
        password: str = "",
    ) -> None:
        self._contact_points = contact_points or ["127.0.0.1"]
        self._port = port
        self._username = username
        self._password = password
        self._cluster = None
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            from cassandra.cluster import Cluster
            from cassandra.auth import PlainTextAuthProvider

            def _connect():
                auth = None
                if self._username:
                    auth = PlainTextAuthProvider(self._username, self._password)
                cluster = Cluster(
                    contact_points=self._contact_points,
                    port=self._port,
                    auth_provider=auth,
                )
                session = cluster.connect()
                session.execute(_DDL_KEYSPACE)
                session.execute(_DDL_TABLE)
                return cluster, session

            self._cluster, self._session = await asyncio.to_thread(_connect)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            tags={"requires": "cassandra-driver", "substrate": "cassandra-wide-row"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()

        def _write():
            session.execute(
                f"INSERT INTO {_KEYSPACE}.{_TABLE} (manifest_id, token, chunk) "
                "VALUES (%s, %s, %s)",
                (manifest_id, token, encoded_chunk),
            )

        await asyncio.to_thread(_write)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        manifest_id = loc.get("manifest", "")
        session = await self._get_session()

        def _read():
            rows = session.execute(
                f"SELECT chunk FROM {_KEYSPACE}.{_TABLE} "
                "WHERE manifest_id=%s AND token=%s",
                (manifest_id, token),
            )
            return list(rows)

        rows = await asyncio.to_thread(_read)
        if not rows:
            raise KeyError(f"cassandra-row chunk not found: {token}")
        chunk = rows[0].chunk
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        manifest_id = loc.get("manifest", "")
        self._reg.pop(token, None)

        session = await self._get_session()

        def _delete():
            session.execute(
                f"DELETE FROM {_KEYSPACE}.{_TABLE} "
                "WHERE manifest_id=%s AND token=%s",
                (manifest_id, token),
            )

        await asyncio.to_thread(_delete)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()

        def _list():
            rows = session.execute(
                f"SELECT token FROM {_KEYSPACE}.{_TABLE} WHERE manifest_id=%s",
                (manifest_id,),
            )
            return list(rows)

        rows = await asyncio.to_thread(_list)
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": r.token, "manifest": manifest_id}),
            )
            for r in rows
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()

            def _ping():
                session.execute("SELECT release_version FROM system.local")

            await asyncio.to_thread(_ping)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._cluster is not None:
            await asyncio.to_thread(self._cluster.shutdown)
            self._cluster = None
            self._session = None
