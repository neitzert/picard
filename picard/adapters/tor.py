"""Tor adapter — proxy chunk operations through Tor to .onion PICARD peers."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Sequence

import httpx

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class TorAdapter(Adapter):
    """Adapter that routes chunk operations through Tor to a .onion peer."""

    def __init__(
        self,
        onion_url: str,
        adapter_name: str,
        *,
        secret: str = "",
        socks_proxy: str = "socks5://127.0.0.1:9050",
        timeout: float = 120.0,
        max_chunk_bytes: int = 64 * 1024,
    ) -> None:
        self._onion_url = onion_url.rstrip("/")
        self._adapter_name = adapter_name
        self._secret = secret
        self._socks_proxy = socks_proxy
        self._timeout = timeout
        self._max_chunk_bytes = max_chunk_bytes
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                from httpx_socks import AsyncProxyTransport
                transport = AsyncProxyTransport.from_url(self._socks_proxy)
            except ImportError:
                raise RuntimeError(
                    "Tor adapter requires 'httpx-socks'. "
                    "Install with: pip install picard[tor]"
                )
            self._client = httpx.AsyncClient(
                transport=transport,
                base_url=self._onion_url,
                timeout=self._timeout,
            )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=f"tor:{self._adapter_name}@{self._onion_url}",
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=True,
            rate_limit_per_min=30,
            tags={"tor": "true", "onion": self._onion_url},
        )

    def _sign(self, body: bytes) -> dict[str, str]:
        if not self._secret:
            return {}
        sig = hmac.new(
            self._secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return {"x-signature": sig}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._get_client()
        payload = {"adapter": self._adapter_name, "data": encoded_chunk}
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/transport/v1/write_chunk",
            content=body,
            headers={"content-type": "application/json", **self._sign(body)},
        )
        resp.raise_for_status()
        result = resp.json()
        return StoredChunkRef(
            adapter_name=result["adapter"],
            location=result["location"],
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._get_client()
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/transport/v1/read_chunk",
            content=body,
            headers={"content-type": "application/json", **self._sign(body)},
        )
        resp.raise_for_status()
        return resp.json()["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._get_client()
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/transport/v1/delete_chunk",
            content=body,
            headers={"content-type": "application/json", **self._sign(body)},
        )
        resp.raise_for_status()
        return resp.json().get("deleted", False)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._get_client()
        payload = {"adapter": self._adapter_name, "manifest_id": manifest_id}
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/transport/v1/list_chunks",
            content=body,
            headers={"content-type": "application/json", **self._sign(body)},
        )
        resp.raise_for_status()
        return [
            StoredChunkRef(adapter_name=r["adapter"], location=r["location"])
            for r in resp.json().get("refs", [])
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get("/transport/v1/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
