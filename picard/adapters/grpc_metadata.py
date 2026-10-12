"""gRPC metadata adapter — stores chunks via a cooperating gRPC-JSON gateway.

Chunk data is passed in both the request body and the ``x-pchk-data``
gRPC metadata header.  The
cooperating service exposes JSON-transcoded gRPC endpoints so no proto
compilation is needed on the client side.

Requires: pip install picard-store[grpc-metadata]  (httpx with h2)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class GrpcMetadataAdapter(Adapter):
    """Scatter chunks into a gRPC service via JSON-transcoded endpoints."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        headers: dict[str, str] = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        try:
            self._client = httpx.AsyncClient(http2=True, headers=headers, timeout=30.0)
        except ImportError:
            self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="grpc-metadata",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        resp = await client.post(
            f"{self._endpoint}/v1/chunks",
            json={"data": encoded_chunk},
            headers={"x-pchk-data": encoded_chunk},
        )
        resp.raise_for_status()
        msg_id = resp.json()["id"]
        return StoredChunkRef(adapter_name="grpc-metadata", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(
            f"{self._endpoint}/v1/chunks/{ref.location}",
        )
        resp.raise_for_status()
        return resp.json()["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(
                f"{self._endpoint}/v1/chunks/{ref.location}",
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        resp = await client.get(
            f"{self._endpoint}/v1/chunks",
            params={"manifest": manifest_id},
        )
        resp.raise_for_status()
        ids = resp.json().get("ids", [])
        return [
            StoredChunkRef(adapter_name="grpc-metadata", location=cid)
            for cid in ids
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(f"{self._endpoint}/v1/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
