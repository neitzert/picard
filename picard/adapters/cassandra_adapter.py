"""Cassandra adapter — stores chunks in Cassandra / ScyllaDB tables.

Hides data in distributed wide-column stores often used for
time-series, IoT, and high-throughput workloads.

Requires: pip install picard[cassandra]  (cassandra-driver)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CassandraAdapter(Adapter):
    """Scatter chunks into a Cassandra table."""

    def __init__(
        self,
        contact_points: list[str] | None = None,
        port: int = 9042,
        keyspace: str = "picard",
        table: str = "chunks",
        username: str | None = None,
        password: str | None = None,
        *,
        max_chunk_bytes: int = 2_097_152,
    ) -> None:
        self._contact_points = contact_points or ["127.0.0.1"]
        self._port = port
        self._keyspace = keyspace
        self._table = table
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._session = None
        self._cluster = None

    def _get_session(self):
        from cassandra.cluster import Cluster
        from cassandra.auth import PlainTextAuthProvider

        if self._session is None:
            auth = None
            if self._username and self._password:
                auth = PlainTextAuthProvider(self._username, self._password)
            self._cluster = Cluster(
                self._contact_points, port=self._port, auth_provider=auth
            )
            self._session = self._cluster.connect(self._keyspace)
        return self._session

    async def close(self) -> None:
        if self._cluster is not None:
            self._cluster.shutdown()
            self._cluster = None
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cassandra",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
            manifest_id = chunk.manifest_id
        except ValueError:
            key = uuid.uuid4().hex
            manifest_id = ""

        def _write():
            session = self._get_session()
            session.execute(
                f"INSERT INTO {self._table} (key, data, manifest_id, created_at) "
                f"VALUES (%s, %s, %s, %s)",
                (key, encoded_chunk, manifest_id, datetime.now(timezone.utc)),
            )

        await asyncio.to_thread(_write)
        loc = f"cassandra://{self._keyspace}/{self._table}/{key}"
        return StoredChunkRef(adapter_name="cassandra", location=loc)

    def _extract_key(self, location: str) -> str:
        prefix = f"cassandra://{self._keyspace}/{self._table}/"
        return location.removeprefix(prefix)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = self._extract_key(ref.location)

        def _read():
            session = self._get_session()
            row = session.execute(
                f"SELECT data FROM {self._table} WHERE key = %s", (key,)
            ).one()
            if row is None:
                raise KeyError(f"chunk not found: {ref.location}")
            return row.data

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = self._extract_key(ref.location)

        def _delete():
            session = self._get_session()
            session.execute(
                f"DELETE FROM {self._table} WHERE key = %s", (key,)
            )

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            session = self._get_session()
            rows = session.execute(
                f"SELECT key FROM {self._table} WHERE manifest_id = %s ALLOW FILTERING",
                (manifest_id,),
            )
            return [
                StoredChunkRef(
                    adapter_name="cassandra",
                    location=f"cassandra://{self._keyspace}/{self._table}/{row.key}",
                )
                for row in rows
            ]

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            session = self._get_session()
            session.execute("SELECT now() FROM system.local")

        try:
            await asyncio.to_thread(_check)
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _stats():
            session = self._get_session()
            rows = session.execute(
                "SELECT mean_partition_size, partitions_count "
                "FROM system.size_estimates "
                f"WHERE keyspace_name = '{self._keyspace}' "
                f"AND table_name = '{self._table}'"
            )
            total_bytes = 0
            total_count = 0
            for row in rows:
                total_bytes += row.mean_partition_size * row.partitions_count
                total_count += row.partitions_count
            return {"used_bytes": total_bytes, "chunk_count": total_count}

        try:
            stats = await asyncio.to_thread(_stats)
            return {
                "used_bytes": stats["used_bytes"],
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": stats["chunk_count"],
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
