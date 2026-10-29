"""Qdrant payload-extra adapter -- stores chunks in a secondary Qdrant collection.

Uses the picard_extra collection (distinct from picard used by qdrant_adapter)
with 128-dimensional vectors and a nested payload structure {"picard": {...}}.

Requires: Qdrant reachable at the configured URL.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "qdrant-payload-extra"
_COLLECTION = "picard_extra"
_DIM = 128


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _token_vector(token: str) -> list[float]:
    import hashlib
    digest = hashlib.sha256(token.encode()).digest()
    floats: list[float] = []
    for i in range(0, _DIM * 4, 4):
        idx = i % len(digest)
        val = digest[idx] / 255.0
        floats.append(val)
    return floats[:_DIM]


class QdrantPayloadExtraAdapter(Adapter):

    def __init__(self, *, url: str = "http://127.0.0.1:6333") -> None:
        self._url = url.rstrip("/")
        self._reg: dict[str, str] = {}
        self._session = None
        self._collection_ready = False

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        session = await self._get_session()
        async with session.get(
            f"{self._url}/collections/{_COLLECTION}"
        ) as resp:
            if resp.status == 200:
                self._collection_ready = True
                return
        async with session.put(
            f"{self._url}/collections/{_COLLECTION}",
            json={
                "vectors": {"size": _DIM, "distance": "Cosine"},
            },
        ) as resp:
            resp.raise_for_status()
        self._collection_ready = True

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "qdrant", "substrate": "qdrant-nested-payload"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_collection()
        token = uuid.uuid4().hex
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        point_id = str(uuid.UUID(token))
        session = await self._get_session()
        async with session.put(
            f"{self._url}/collections/{_COLLECTION}/points",
            json={
                "points": [{
                    "id": point_id,
                    "vector": _token_vector(token),
                    "payload": {
                        "picard": {
                            "token": token,
                            "data": encoded_chunk,
                            "manifest": manifest_id,
                        },
                    },
                }],
            },
        ) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "point_id": point_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        session = await self._get_session()
        async with session.get(
            f"{self._url}/collections/{_COLLECTION}/points/{loc['point_id']}",
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["result"]["payload"]["picard"]["data"]
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.post(
            f"{self._url}/collections/{_COLLECTION}/points/delete",
            json={"points": [loc["point_id"]]},
        ) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        await self._ensure_collection()
        session = await self._get_session()
        async with session.post(
            f"{self._url}/collections/{_COLLECTION}/points/scroll",
            json={
                "filter": {
                    "must": [{
                        "key": "picard.manifest",
                        "match": {"value": manifest_id},
                    }]
                },
                "with_payload": True,
                "limit": 256,
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        refs = []
        for point in data.get("result", {}).get("points", []):
            token = point["payload"]["picard"]["token"]
            refs.append(StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": token, "point_id": point["id"],
                                      "manifest": manifest_id}),
            ))
        return refs

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(f"{self._url}/healthz") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
