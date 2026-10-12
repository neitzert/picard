"""etcd adapter — stores chunks as key-value pairs via the etcd v3 JSON API.

Each chunk becomes an etcd key under a structured prefix. Uses the gRPC-gateway
JSON/HTTP API so no gRPC dependency is needed — just httpx.

Requires: pip install picard-store[etcd]  (uses httpx)
"""

from __future__ import annotations

import base64
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


def _b64e(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _b64d(s: str) -> str:
    return base64.b64decode(s).decode()


def _prefix_range_end(prefix: str) -> str:
    b = bytearray(prefix.encode())
    b[-1] += 1
    return base64.b64encode(bytes(b)).decode()


class EtcdAdapter(Adapter):
    """Scatter chunks into etcd v3 via the gRPC-gateway JSON API."""

    def __init__(
        self,
        endpoint: str = "http://localhost:2379",
        *,
        prefix: str = "/picard/",
        ttl_seconds: int | None = None,
        max_chunk_bytes: int = 1_500_000,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx
        self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}"

    async def _grant_lease(self) -> int | None:
        if self._ttl is None:
            return None
        client = await self._ensure_client()
        resp = await client.post(
            f"{self._endpoint}/v3/lease/grant",
            json={"TTL": self._ttl},
        )
        return int(resp.json()["ID"])

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="etcd",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            ttl_seconds=self._ttl,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)

        body: dict = {
            "key": _b64e(key),
            "value": _b64e(encoded_chunk),
        }
        lease_id = await self._grant_lease()
        if lease_id is not None:
            body["lease"] = str(lease_id)

        await client.post(f"{self._endpoint}/v3/kv/put", json=body)
        return StoredChunkRef(adapter_name="etcd", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.post(
            f"{self._endpoint}/v3/kv/range",
            json={"key": _b64e(ref.location)},
        )
        data = resp.json()
        kvs = data.get("kvs", [])
        if not kvs:
            raise ValueError(f"key not found: {ref.location}")
        return _b64d(kvs[0]["value"])

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self._endpoint}/v3/kv/deleterange",
                json={"key": _b64e(ref.location)},
            )
            deleted = int(resp.json().get("deleted", 0))
            return deleted > 0
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        prefix = f"{self._prefix}{manifest_id}/"
        resp = await client.post(
            f"{self._endpoint}/v3/kv/range",
            json={
                "key": _b64e(prefix),
                "range_end": _prefix_range_end(prefix),
            },
        )
        data = resp.json()
        kvs = data.get("kvs", [])
        return [
            StoredChunkRef(
                adapter_name="etcd",
                location=_b64d(kv["key"]),
            )
            for kv in kvs
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._endpoint}/health")
            body = resp.json()
            return body.get("health") == "true" or body.get("status") == "true"
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
