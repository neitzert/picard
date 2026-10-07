"""Remote adapter — proxy chunk operations to a remote PICARD peer."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Sequence

import httpx

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class RemoteAdapter(Adapter):
    """Adapter that delegates to a remote PICARD transport API."""

    def __init__(
        self,
        peer_url: str,
        adapter_name: str,
        *,
        secret: str = "",
        timeout: float = 30.0,
    ) -> None:
        self._peer_url = peer_url.rstrip("/")
        self._adapter_name = adapter_name
        self._secret = secret
        self._timeout = timeout

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=f"remote:{self._adapter_name}@{self._peer_url}",
            max_chunk_bytes=64 * 1024,
            tags={"remote": "true", "peer": self._peer_url},
        )

    def _sign(self, body: bytes) -> dict[str, str]:
        if not self._secret:
            return {}
        sig = hmac.new(
            self._secret.encode(), body, hashlib.sha256,
        ).hexdigest()
        return {"x-signature": sig}

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        payload = {"adapter": self._adapter_name, "data": encoded_chunk}
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._peer_url}/transport/v1/write_chunk",
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
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._peer_url}/transport/v1/read_chunk",
                content=body,
                headers={"content-type": "application/json", **self._sign(body)},
            )
            resp.raise_for_status()
            return resp.json()["data"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        payload = {"adapter": self._adapter_name, "ref": ref.location}
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._peer_url}/transport/v1/delete_chunk",
                content=body,
                headers={"content-type": "application/json", **self._sign(body)},
            )
            resp.raise_for_status()
            return resp.json().get("deleted", False)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        payload = {"adapter": self._adapter_name, "manifest_id": manifest_id}
        body = json.dumps(payload).encode()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._peer_url}/transport/v1/list_chunks",
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
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._peer_url}/transport/v1/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._peer_url}/transport/v1/health")
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
