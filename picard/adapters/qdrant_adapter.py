"""Qdrant adapter -- stores chunks as Qdrant vector point payloads.

Each write_chunk creates a point in a Qdrant collection. The encoded chunk
is stored in the point payload field 'picard_data'. Vector is a unit hash
derived from the chunk content.

Requires: pip install qdrant-client
"""
from __future__ import annotations

import hashlib
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "qdrant"
_COLLECTION = "picard_chunks"
_DIM = 64  # vector dimension


def _vec_from_chunk(encoded_chunk: str) -> list[float]:
    # shake_256 gives exactly DIM*4 bytes regardless of _DIM
    h = hashlib.shake_256(encoded_chunk.encode()).digest(_DIM * 4)
    floats = [struct.unpack_from(">f", h, i * 4)[0] for i in range(_DIM)]
    norm = (sum(f * f for f in floats) ** 0.5) or 1.0
    return [f / norm for f in floats]


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class QdrantAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6333) -> None:
        self._host = host
        self._port = port
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import Distance, VectorParams
            self._client = AsyncQdrantClient(host=self._host, port=self._port)
            try:
                await self._client.get_collection(_COLLECTION)
            except Exception:
                await self._client.create_collection(
                    collection_name=_COLLECTION,
                    vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
                )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "qdrant-client", "substrate": "qdrant-point-payload"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._get_client()
        from qdrant_client.models import PointStruct
        point_id = str(uuid.uuid4())
        vec = _vec_from_chunk(encoded_chunk)
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        await client.upsert(
            collection_name=_COLLECTION,
            points=[PointStruct(
                id=point_id,
                vector=vec,
                payload={"picard_data": encoded_chunk,
                         "picard_manifest": manifest_id},
            )],
        )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"point_id": point_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        point_id = json.loads(ref.location)["point_id"]
        client = await self._get_client()
        results = await client.retrieve(
            collection_name=_COLLECTION,
            ids=[point_id],
            with_payload=True,
        )
        if not results:
            raise KeyError(f"qdrant chunk not found: {point_id}")
        return results[0].payload["picard_data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        point_id = json.loads(ref.location)["point_id"]
        client = await self._get_client()
        from qdrant_client.models import PointIdsList
        await client.delete(
            collection_name=_COLLECTION,
            points_selector=PointIdsList(points=[point_id]),
        )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        results, _ = await client.scroll(
            collection_name=_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="picard_manifest",
                               match=MatchValue(value=manifest_id)),
            ]),
            with_payload=False,
            limit=10000,
        )
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"point_id": str(r.id)}))
                for r in results]

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            info = await client.get_collections()
            return info is not None
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
