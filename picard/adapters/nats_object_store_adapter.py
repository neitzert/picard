"""NATS Object Store adapter -- stores chunks in a NATS JetStream Object Store bucket.

Each write_chunk puts the encoded chunk as a named object in an Object Store
bucket. Different from nats_jetstream_adapter.py (which uses a KV-stream
substrate): Object Store targets NATS OBJ_* streams designed for large binary
objects with chunked streaming.

Requires: pip install nats-py
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "nats-object-store"
_BUCKET = "picard-objects"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class NatsObjectStoreAdapter(Adapter):

    def __init__(self, *, servers: str = "nats://127.0.0.1:4222") -> None:
        self._servers = servers
        self._nc = None
        self._ob = None
        self._reg: dict[str, str] = {}

    async def _get_ob(self):
        if self._nc is None:
            import nats
            self._nc = await nats.connect(servers=self._servers)
            js = self._nc.jetstream()
            try:
                self._ob = await js.find_object_store(_BUCKET)
            except Exception:
                self._ob = await js.create_object_store(_BUCKET)
        return self._ob

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=8_388_608,
            persistent=True,
            ordered=False,
            tags={"requires": "nats-py", "substrate": "nats-object-store"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        ob = await self._get_ob()
        data = encoded_chunk.encode("utf-8")
        await ob.put(token, data)
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
        ob = await self._get_ob()
        result = await ob.get(token)
        chunk = result.decode("utf-8") if isinstance(result, bytes) else result
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)
        try:
            ob = await self._get_ob()
            await ob.delete(token)
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            await self._get_ob()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._ob = None
