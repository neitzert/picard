"""Remote adapter — proxy chunk operations to a remote PICARD peer."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Sequence
from urllib.parse import urlparse

import httpx

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_ALLOWED_SCHEMES = {"http", "https"}


class RemoteAdapter(Adapter):
    """Adapter that delegates to a remote PICARD transport API."""

    def __init__(
        self,
        peer_url: str,
        adapter_name: str,
        *,
        secret: str = "",
        timeout: float = 30.0,
        max_chunk_bytes: int = 64 * 1024,
    ) -> None:
        parsed = urlparse(peer_url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"peer_url scheme must be http or https, got {parsed.scheme!r}"
            )
        self._peer_url = peer_url.rstrip("/")
        self._adapter_name = adapter_name
        self._secret = secret
        self._timeout = timeout
        self._max_chunk_bytes = max_chunk_bytes
        self._client = httpx.AsyncClient(
            base_url=self._peer_url, timeout=self._timeout,
        )

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=f"remote:{self._adapter_name}@{self._peer_url}",
            max_chunk_bytes=self._max_chunk_bytes,
            tags={"remote": "true", "peer": self._peer_url},
        )

    def _sign(self, body: bytes, path: str) -> dict[str, str]:
        if not self._secret:
            return {}
        ts = str(time.time())
        msg = path.encode() + b"\n" + ts.encode() + b"\n" + body
        sig = hmac.new(
            self._secret.encode(), msg, hashlib.sha256,
        ).hexdigest()
        return {"x-signature": sig, "x-timestamp": ts}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        path = "/transport/v1/write_chunk"
        payload = {"adapter": self._adapter_name, "data": encoded_chunk}
        body = json.dumps(payload).encode()
        resp = await self._client.post(
            path,
            content=body,
            headers={"content-type": "application/json", **self._sign(body, path)},
        )
        resp.raise_for_status()
        result = resp.json()
        return StoredChunkRef(
            adapter_name=result["adapter"],
            location=result["location"],
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = "/transport/v1/read_chunk"
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        resp = await self._client.post(
            path,
            content=body,
            headers={"content-type": "application/json", **self._sign(body, path)},
        )
        resp.raise_for_status()
        return resp.json()["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = "/transport/v1/delete_chunk"
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        resp = await self._client.post(
            path,
            content=body,
            headers={"content-type": "application/json", **self._sign(body, path)},
        )
        resp.raise_for_status()
        return resp.json().get("deleted", False)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        path = "/transport/v1/list_chunks"
        payload = {"adapter": self._adapter_name, "manifest_id": manifest_id}
        body = json.dumps(payload).encode()
        resp = await self._client.post(
            path,
            content=body,
            headers={"content-type": "application/json", **self._sign(body, path)},
        )
        resp.raise_for_status()
        return [
            StoredChunkRef(adapter_name=r["adapter"], location=r["location"])
            for r in resp.json().get("refs", [])
        ]

    async def health_check(self) -> bool:
        path = "/transport/v1/health"
        try:
            resp = await self._client.get(
                path, headers=self._sign(b"", path),
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        path = "/transport/v1/health"
        try:
            resp = await self._client.get(
                path, headers=self._sign(b"", path),
            )
            data = resp.json()
            caps = data.get("capacities", {})
            remote_cap = caps.get(self._adapter_name)
            if isinstance(remote_cap, dict):
                return remote_cap
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }

    async def close(self) -> None:
        await self._client.aclose()
