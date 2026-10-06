"""Neo4j adapter — stores chunks as graph nodes.

Hides data in a graph database. Each chunk becomes a :Chunk node
with key, data, and manifest_id properties.

Requires: pip install picard[neo4j]  (neo4j)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class Neo4jAdapter(Adapter):
    """Scatter chunks into Neo4j graph nodes."""

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        username: str = "neo4j",
        password: str = "neo4j",
        database: str = "neo4j",
        *,
        max_chunk_bytes: int = 2_097_152,
    ) -> None:
        self._uri = uri
        self._username = username
        self._password = password
        self._database = database
        self._max = max_chunk_bytes
        self._driver = None

    def _get_driver(self):
        import neo4j

        if self._driver is None:
            self._driver = neo4j.GraphDatabase.driver(
                self._uri, auth=(self._username, self._password)
            )
        return self._driver

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="neo4j",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
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
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                session.run(
                    "MERGE (c:Chunk {key: $key}) "
                    "SET c.data = $data, c.manifest_id = $mid",
                    key=key, data=encoded_chunk, mid=manifest_id,
                )

        await asyncio.to_thread(_write)
        loc = f"neo4j://{self._database}/Chunk/{key}"
        return StoredChunkRef(adapter_name="neo4j", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("/Chunk/", 1)[-1]

        def _read():
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                result = session.run(
                    "MATCH (c:Chunk {key: $key}) RETURN c.data AS data",
                    key=key,
                )
                record = result.single()
                if record is None:
                    raise KeyError(f"chunk not found: {ref.location}")
                return record["data"]

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("/Chunk/", 1)[-1]

        def _delete():
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                result = session.run(
                    "MATCH (c:Chunk {key: $key}) DELETE c RETURN count(c) AS cnt",
                    key=key,
                )
                record = result.single()
                return record["cnt"] > 0

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                result = session.run(
                    "MATCH (c:Chunk {manifest_id: $mid}) RETURN c.key AS key",
                    mid=manifest_id,
                )
                return [
                    StoredChunkRef(
                        adapter_name="neo4j",
                        location=f"neo4j://{self._database}/Chunk/{record['key']}",
                    )
                    for record in result
                ]

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                session.run("RETURN 1")

        try:
            await asyncio.to_thread(_check)
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _stats():
            driver = self._get_driver()
            with driver.session(database=self._database) as session:
                result = session.run("MATCH (c:Chunk) RETURN count(c) AS cnt")
                record = result.single()
                return record["cnt"]

        try:
            count = await asyncio.to_thread(_stats)
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": count,
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
