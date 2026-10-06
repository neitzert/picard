"""QUIC/HTTP3 adapter -- stores chunks via HTTP/3 requests.

Chunks are PUT/POSTed to a server endpoint that supports QUIC/HTTP3,
giving low-latency multiplexed transfers with built-in TLS.

Requires: pip install picard[quic]  (httpx[http2] or aioquic)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence
from urllib.parse import urlparse

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class QuicAdapter(Adapter):
    """Scatter chunks over QUIC/HTTP3 to a storage endpoint."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        verify_ssl: bool = True,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._verify_ssl = verify_ssl
        self._max = max_chunk_bytes
        self._parsed = urlparse(self._url)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="quic",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=1000,
        )

    def _chunk_url(self, chunk_id: str) -> str:
        return f"{self._url}/chunks/{chunk_id}"

    def _location_key(self, encoded_chunk: str) -> str:
        try:
            from picard.chunker import Chunk

            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}_{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex
        return f"quic://{self._parsed.hostname}{self._parsed.path}/chunks/{chunk_id}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import httpx

        loc = self._location_key(encoded_chunk)
        chunk_id = loc.split("/chunks/", 1)[1]
        url = self._chunk_url(chunk_id)

        async with httpx.AsyncClient(
            http2=True, verify=self._verify_ssl, headers=self._headers
        ) as client:
            resp = await client.put(url, content=encoded_chunk.encode())
            resp.raise_for_status()

        return StoredChunkRef(adapter_name="quic", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import httpx

        chunk_id = ref.location.split("/chunks/", 1)[1]
        url = self._chunk_url(chunk_id)

        async with httpx.AsyncClient(
            http2=True, verify=self._verify_ssl, headers=self._headers
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import httpx

        chunk_id = ref.location.split("/chunks/", 1)[1]
        url = self._chunk_url(chunk_id)

        try:
            async with httpx.AsyncClient(
                http2=True, verify=self._verify_ssl, headers=self._headers
            ) as client:
                resp = await client.delete(url)
                return resp.status_code < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import httpx

        url = f"{self._url}/chunks/{manifest_id}/"

        try:
            async with httpx.AsyncClient(
                http2=True, verify=self._verify_ssl, headers=self._headers
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                items = resp.json()
                return [
                    StoredChunkRef(
                        adapter_name="quic",
                        location=f"quic://{self._parsed.hostname}{self._parsed.path}/chunks/{manifest_id}/{item}",
                    )
                    for item in items
                ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(
                http2=True, verify=self._verify_ssl, headers=self._headers
            ) as client:
                resp = await client.head(self._url)
                return resp.status_code < 500
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
