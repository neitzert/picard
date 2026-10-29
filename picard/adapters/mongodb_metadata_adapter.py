"""MongoDB metadata adapter -- stores chunks hidden in a document metadata subdocument.

The document's top-level payload field holds a SHA-256 digest; the actual chunk
lives in meta.picard, obscuring it from naive collection scans.

Requires: pip install motor
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mongodb-metadata"
_COLL = "objects"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class MongodbMetadataAdapter(Adapter):

    def __init__(self, *, url: str = "mongodb://127.0.0.1:27017",
                 dbname: str = "picard") -> None:
        self._url = url
        self._dbname = dbname
        self._client = None
        self._coll = None
        self._reg: dict[str, str] = {}

    async def _get_coll(self):
        if self._coll is None:
            from motor.motor_asyncio import AsyncIOMotorClient
            self._client = AsyncIOMotorClient(self._url)
            self._coll = self._client[self._dbname][_COLL]
        return self._coll

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "motor", "substrate": "mongodb-metadata"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        digest = hashlib.sha256(encoded_chunk.encode()).hexdigest()
        mid = _manifest_id(encoded_chunk)
        coll = await self._get_coll()
        await coll.insert_one({
            "_id": token,
            "payload": digest,
            "meta": {"picard": encoded_chunk},
            "manifest": mid,
        })
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token in self._reg:
            return self._reg[token]
        coll = await self._get_coll()
        doc = await coll.find_one({"_id": token})
        if doc is None:
            raise KeyError(f"mongodb-metadata chunk not found: {token}")
        chunk = doc["meta"]["picard"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        coll = await self._get_coll()
        await coll.delete_one({"_id": token})
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        coll = await self._get_coll()
        refs = []
        async for doc in coll.find({"manifest": manifest_id}):
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": doc["_id"]}),
            ))
        return refs

    async def health_check(self) -> bool:
        try:
            coll = await self._get_coll()
            await coll.database.client.admin.command("ping")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._coll = None
