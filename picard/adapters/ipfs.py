"""IPFS adapter — stores chunks as IPFS objects via the Kubo HTTP API.

Each chunk is added as a file, optionally pinned, and tracked via
IPFS MFS (Mutable File System) for efficient listing by manifest.

Requires: pip install picard[ipfs]   (just httpx)
"""

from __future__ import annotations

from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class IpfsAdapter(Adapter):
    """Scatter chunks into IPFS via the Kubo HTTP API."""

    def __init__(
        self,
        api_url: str = "http://localhost:5001",
        *,
        pin: bool = True,
        max_chunk_bytes: int = 250_000,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._pin = pin
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(base_url=self._api_url, timeout=30.0)
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ipfs",
            max_chunk_bytes=self._max,
            persistent=self._pin,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)

        resp = await client.post(
            "/api/v0/add",
            files={"file": ("chunk.txt", encoded_chunk.encode())},
        )
        data = resp.json()
        cid = data["Hash"]

        if self._pin:
            await client.post("/api/v0/pin/add", params={"arg": cid})

        await client.post(
            "/api/v0/files/mkdir",
            params={"arg": f"/picard/{chunk.manifest_id}", "parents": "true"},
        )
        await client.post(
            "/api/v0/files/cp",
            params={"arg": [f"/ipfs/{cid}", f"/picard/{chunk.manifest_id}/{cid}"]},
        )

        return StoredChunkRef(adapter_name="ipfs", location=cid)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.post("/api/v0/cat", params={"arg": ref.location})
        return resp.text

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            if self._pin:
                await client.post("/api/v0/pin/rm", params={"arg": ref.location})

            encoded = (await client.post(
                "/api/v0/cat", params={"arg": ref.location}
            )).text
            chunk = Chunk.decode(encoded)
            await client.post(
                "/api/v0/files/rm",
                params={"arg": f"/picard/{chunk.manifest_id}/{ref.location}"},
            )
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        try:
            resp = await client.post(
                "/api/v0/files/ls",
                params={"arg": f"/picard/{manifest_id}", "long": "true"},
            )
            data = resp.json()
        except Exception:
            return []

        entries = data.get("Entries") or []
        return [
            StoredChunkRef(adapter_name="ipfs", location=entry["Name"])
            for entry in entries
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.post("/api/v0/id")
            return resp.status_code == 200 and "ID" in resp.json()
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
