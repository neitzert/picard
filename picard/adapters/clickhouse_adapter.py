"""ClickHouse adapter — stores chunks in a column-oriented analytics DB.

Stores data in analytics infrastructure. ClickHouse is optimized for
fast reads over huge datasets, making it a high-throughput backend for
chunk storage.

Requires: pip install picard-store[clickhouse]  (aiohttp)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ClickHouseAdapter(Adapter):
    """Scatter chunks into ClickHouse rows."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "picard",
        username: str = "default",
        password: str = "",
        *,
        table: str = "chunks",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._database = database
        self._table = table
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="clickhouse",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _params(self, query: str) -> dict:
        return {
            "database": self._database,
            "query": query,
            "user": self._username,
            "password": self._password,
        }

    async def _ensure_table(self, session: aiohttp.ClientSession) -> None:
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "key String, data String, manifest_id String"
            ") ENGINE = MergeTree() ORDER BY key"
        )
        async with session.post(
            self._base_url, params=self._params(ddl)
        ) as resp:
            resp.raise_for_status()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
            manifest_id = chunk.manifest_id
        except ValueError:
            key = uuid.uuid4().hex
            manifest_id = ""

        row = json.dumps({"key": key, "data": encoded_chunk, "manifest_id": manifest_id})
        query = f"INSERT INTO {self._table} FORMAT JSONEachRow"

        session = await self._get_session()
        await self._ensure_table(session)
        async with session.post(
            self._base_url, params=self._params(query), data=row
        ) as resp:
            resp.raise_for_status()

        loc = f"clickhouse://{self._database}/{self._table}/{key}"
        return StoredChunkRef(adapter_name="clickhouse", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("/", 3)[-1]
        query = (
            f"SELECT data FROM {self._table} WHERE key = '{key}' "
            "FORMAT JSONEachRow"
        )

        session = await self._get_session()
        async with session.get(
            self._base_url, params=self._params(query)
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            if not text.strip():
                raise KeyError(f"chunk not found: {ref.location}")
            return json.loads(text.strip().split("\n")[0])["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("/", 3)[-1]
        query = f"ALTER TABLE {self._table} DELETE WHERE key = '{key}'"

        try:
            session = await self._get_session()
            async with session.post(
                self._base_url, params=self._params(query)
            ) as resp:
                resp.raise_for_status()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        query = (
            f"SELECT key FROM {self._table} "
            f"WHERE manifest_id = '{manifest_id}' FORMAT JSONEachRow"
        )

        session = await self._get_session()
        async with session.get(
            self._base_url, params=self._params(query)
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            refs = []
            for line in text.strip().split("\n"):
                if line:
                    k = json.loads(line)["key"]
                    loc = f"clickhouse://{self._database}/{self._table}/{k}"
                    refs.append(StoredChunkRef(adapter_name="clickhouse", location=loc))
            return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                self._base_url, params=self._params("SELECT 1")
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        query = (
            f"SELECT count() AS cnt, sum(length(data)) AS bytes "
            f"FROM {self._table} FORMAT JSONEachRow"
        )
        try:
            session = await self._get_session()
            async with session.get(
                self._base_url, params=self._params(query)
            ) as resp:
                resp.raise_for_status()
                row = json.loads((await resp.text()).strip())
                return {
                    "used_bytes": row["bytes"],
                    "free_bytes": None,
                    "total_bytes": None,
                    "chunk_count": row["cnt"],
                }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
