"""NATS JetStream KV adapter -- stores chunks in a NATS JetStream Key-Value bucket.

Each write_chunk puts the encoded chunk in bucket 'picard' under a uuid key.
Cold reads fetch by key. Requires NATS server with JetStream enabled.

Requires: pip install nats-py
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "nats_kv"
_BUCKET = "picard"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class NatsKvAdapter(Adapter):

    def __init__(self, *, url: str = "nats://127.0.0.1:4222") -> None:
        self._url = url
        self._nc = None
        self._kv = None

    async def _get_kv(self):
        if self._kv is None:
            import nats
            self._nc = await nats.connect(self._url)
            js = self._nc.jetstream()
            try:
                self._kv = await js.key_value(_BUCKET)
            except Exception:
                from nats.js.api import KeyValueConfig
                self._kv = await js.create_key_value(KeyValueConfig(bucket=_BUCKET))
        return self._kv

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            tags={"requires": "nats-py", "substrate": "nats-jetstream-kv"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        kv = await self._get_kv()
        key = f"chunk.{uuid.uuid4().hex[:16]}"
        await kv.put(key, encoded_chunk.encode("utf-8"))
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"key": key}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        kv = await self._get_kv()
        key = json.loads(ref.location)["key"]
        entry = await kv.get(key)
        if entry is None or entry.value is None:
            raise KeyError(f"nats_kv key not found: {key}")
        return entry.value.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        kv = await self._get_kv()
        key = json.loads(ref.location)["key"]
        await kv.delete(key)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        kv = await self._get_kv()
        keys = await kv.keys()
        refs = []
        for key in keys:
            try:
                entry = await kv.get(key)
                if entry is None or entry.value is None:
                    continue
                chunk = entry.value.decode("utf-8")
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"key": key})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            await self._get_kv()
            return self._nc is not None and self._nc.is_connected
        except Exception:
            return False

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._kv = None
