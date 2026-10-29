"""NATS Object Store adapter -- stores chunks in a NATS JetStream Object Store.

Each write_chunk puts the encoded chunk as bytes in bucket 'picard_objects'
under a uuid-prefixed key. Suitable for larger chunks than KV.

Requires: pip install nats-py
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "nats_object_store"
_BUCKET = "picard_objects"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class NatsObjectStoreAdapter(Adapter):

    def __init__(self, *, url: str = "nats://127.0.0.1:4222") -> None:
        self._url = url
        self._nc = None
        self._os = None

    async def _get_store(self):
        if self._os is None:
            import nats
            self._nc = await nats.connect(self._url)
            js = self._nc.jetstream()
            try:
                self._os = await js.object_store(_BUCKET)
            except Exception:
                self._os = await js.create_object_store(_BUCKET)
        return self._os

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=50_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "nats-py", "substrate": "nats-object-store"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        store = await self._get_store()
        name = f"chunk.{uuid.uuid4().hex[:16]}"
        data = encoded_chunk.encode("utf-8")
        await store.put(name, data)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"name": name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        store = await self._get_store()
        name = json.loads(ref.location)["name"]
        result = await store.get(name)
        if result is None:
            raise KeyError(f"nats_object_store object not found: {name}")
        return result.data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        store = await self._get_store()
        name = json.loads(ref.location)["name"]
        await store.delete(name)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        store = await self._get_store()
        try:
            infos = await store.list()
        except Exception:
            return []
        refs = []
        for info in infos:
            if not info.name.startswith("chunk."):
                continue
            try:
                result = await store.get(info.name)
                if result is None:
                    continue
                chunk = result.data.decode("utf-8")
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"name": info.name})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._get_store()
            return self._nc is not None and self._nc.is_connected
        except Exception:
            return False

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._os = None
