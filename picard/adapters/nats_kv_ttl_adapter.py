"""NATS KV TTL adapter -- stores chunks in a NATS JetStream KV bucket with TTL.

Uses a dedicated bucket PICARD_TTL with a 24-hour TTL. Each write_chunk
stores the encoded chunk under the key picard.{token}. This complements
nats_kv_adapter (which uses PICARD bucket with no TTL).

Requires: pip install nats-py
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "nats-kv-ttl"
_BUCKET = "PICARD_TTL"
_TTL_SECONDS = 86400


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class NatsKvTtlAdapter(Adapter):

    def __init__(self, *, servers: str = "nats://127.0.0.1:4222") -> None:
        self._servers = servers
        self._nc = None
        self._kv = None
        self._reg: dict[str, str] = {}

    async def _get_kv(self):
        if self._nc is None:
            import nats
            self._nc = await nats.connect(servers=self._servers)
            js = self._nc.jetstream()
            try:
                self._kv = await js.key_value(_BUCKET)
            except Exception:
                self._kv = await js.create_key_value(
                    bucket=_BUCKET,
                    ttl=_TTL_SECONDS,
                )
        return self._kv

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            ttl_seconds=_TTL_SECONDS,
            rate_limit_per_min=10000,
            tags={"requires": "nats-py", "substrate": "nats-kv-ttl-bucket"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id(encoded_chunk)
        key = f"picard.{token}"
        kv = await self._get_kv()
        await kv.put(key, encoded_chunk.encode("utf-8"))
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        kv = await self._get_kv()
        entry = await kv.get(loc["key"])
        chunk = entry.value.decode("utf-8")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        try:
            kv = await self._get_kv()
            await kv.delete(loc["key"])
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                          "key": f"picard.{token}",
                                          "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            await self._get_kv()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._kv = None
