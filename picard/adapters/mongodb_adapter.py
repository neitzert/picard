"""MongoDB adapter — stores chunks as documents in a MongoDB collection.

Each chunk becomes a document with the encoded data as a string field.

Requires: pip install picard-store[mongodb]  (motor)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MongoDBAdapter(Adapter):
    """Scatter chunks into MongoDB documents."""

    def __init__(
        self,
        connection_string: str = "mongodb://localhost:27017",
        database: str = "picard",
        collection: str = "chunks",
        *,
        max_chunk_bytes: int = 16_777_216,
    ) -> None:
        self._conn_str = connection_string
        self._db_name = database
        self._coll_name = collection
        self._max = max_chunk_bytes
        self._client = None

    def _get_collection(self):
        import motor.motor_asyncio

        if self._client is None:
            self._client = motor.motor_asyncio.AsyncIOMotorClient(self._conn_str)
        return self._client[self._db_name][self._coll_name]

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="mongodb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
            manifest_id = chunk.manifest_id
        except ValueError:
            key = uuid.uuid4().hex
            manifest_id = ""

        coll = self._get_collection()
        doc = {
            "_id": key,
            "data": encoded_chunk,
            "manifest_id": manifest_id,
        }
        await coll.replace_one({"_id": key}, doc, upsert=True)
        loc = f"mongo://{self._db_name}/{self._coll_name}/{key}"
        return StoredChunkRef(adapter_name="mongodb", location=loc)

    def _extract_key(self, location: str) -> str:
        prefix = f"mongo://{self._db_name}/{self._coll_name}/"
        return location.removeprefix(prefix)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = self._extract_key(ref.location)
        coll = self._get_collection()
        doc = await coll.find_one({"_id": key})
        if doc is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return doc["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = self._extract_key(ref.location)
        coll = self._get_collection()
        result = await coll.delete_one({"_id": key})
        return result.deleted_count > 0

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        coll = self._get_collection()
        refs = []
        async for doc in coll.find({"manifest_id": manifest_id}):
            loc = f"mongo://{self._db_name}/{self._coll_name}/{doc['_id']}"
            refs.append(StoredChunkRef(adapter_name="mongodb", location=loc))
        return refs

    async def health_check(self) -> bool:
        try:
            coll = self._get_collection()
            await coll.database.client.server_info()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            coll = self._get_collection()
            stats = await coll.database.command("dbStats")
            return {
                "used_bytes": stats.get("dataSize", 0),
                "free_bytes": None,
                "total_bytes": stats.get("storageSize", None),
                "chunk_count": await coll.count_documents({}),
            }
        except Exception:
            return {"used_bytes": None, "free_bytes": None, "total_bytes": None, "chunk_count": None}
