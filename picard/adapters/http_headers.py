"""HTTP headers adapter — stores chunks in custom HTTP headers.

A cooperating HTTP server accepts chunks via X-PCHK-Data headers
on POST requests and returns them via the same header on GET.
Uses custom HTTP headers as a storage channel.

Requires: pip install picard-store[http-headers]  (httpx)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class HttpHeadersAdapter(Adapter):
    """Scatter chunks into HTTP custom headers on a cooperating server."""

    def __init__(
        self,
        base_url: str,
        auth_token: str | None = None,
        *,
        max_chunk_bytes: int = 7000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        self._client = httpx.AsyncClient(headers=headers, timeout=30.0)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="http-headers",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        msg_id = uuid.uuid4().hex[:16]
        resp = await client.post(
            f"{self._base_url}/store",
            headers={
                "X-PCHK-Data": encoded_chunk,
                "X-PCHK-Id": msg_id,
            },
        )
        resp.raise_for_status()
        return StoredChunkRef(adapter_name="http-headers", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(f"{self._base_url}/retrieve/{ref.location}")
        resp.raise_for_status()
        data = resp.headers.get("X-PCHK-Data")
        if data is None:
            raise ValueError(f"no X-PCHK-Data header in response for {ref.location}")
        return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(f"{self._base_url}/store/{ref.location}")
            return resp.status_code == 204
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        try:
            resp = await client.get(
                f"{self._base_url}/list",
                params={"manifest": manifest_id},
            )
            resp.raise_for_status()
            ids = resp.json()
            return [
                StoredChunkRef(adapter_name="http-headers", location=chunk_id)
                for chunk_id in ids
            ]
        except Exception:
            return []

    async def health_check(self) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self._base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
