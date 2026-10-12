"""NATS adapter -- stores chunks in NATS JetStream key-value buckets.

Each chunk becomes a KV entry keyed by manifest/block/chunk index.
JetStream provides persistence and replay.

Requires: pip install picard-store[nats]  (nats-py)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NatsAdapter(Adapter):
    """Scatter chunks into a NATS JetStream KV bucket."""

    def __init__(
        self,
        servers: str | list[str] = "nats://localhost:4222",
        *,
        bucket: str = "picard-chunks",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        if isinstance(servers, str):
            servers = [servers]
        self._servers = servers
        self._bucket = bucket
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="nats",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=5000,
        )

    def _kv_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}.{chunk.block_index}.{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def _connect(self):
        import nats

        return await nats.connect(servers=self._servers)

    async def _get_kv(self, nc):
        js = nc.jetstream()
        try:
            return await js.key_value(bucket=self._bucket)
        except Exception:
            return await js.create_key_value(bucket=self._bucket)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._kv_key(encoded_chunk)
        nc = await self._connect()
        try:
            kv = await self._get_kv(nc)
            await kv.put(key, encoded_chunk.encode("utf-8"))
            location = f"nats://{self._bucket}/{key}"
        finally:
            await nc.close()
        return StoredChunkRef(adapter_name="nats", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("nats://").split("/", 1)
        key = parts[1] if len(parts) > 1 else parts[0]

        nc = await self._connect()
        try:
            kv = await self._get_kv(nc)
            entry = await kv.get(key)
            return entry.value.decode("utf-8")
        finally:
            await nc.close()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix("nats://").split("/", 1)
        key = parts[1] if len(parts) > 1 else parts[0]

        nc = await self._connect()
        try:
            kv = await self._get_kv(nc)
            await kv.delete(key)
            return True
        except Exception:
            return False
        finally:
            await nc.close()

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        nc = await self._connect()
        try:
            kv = await self._get_kv(nc)
            keys = await kv.keys()
            prefix = f"{manifest_id}."
            return [
                StoredChunkRef(
                    adapter_name="nats",
                    location=f"nats://{self._bucket}/{k}",
                )
                for k in keys
                if k.startswith(prefix)
            ]
        except Exception:
            return []
        finally:
            await nc.close()

    async def health_check(self) -> bool:
        try:
            nc = await self._connect()
            await nc.close()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            nc = await self._connect()
            try:
                kv = await self._get_kv(nc)
                status = await kv.status()
                return {
                    "used_bytes": getattr(status, "bytes", None),
                    "free_bytes": None,
                    "total_bytes": None,
                    "chunk_count": getattr(status, "values", None),
                }
            finally:
                await nc.close()
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
