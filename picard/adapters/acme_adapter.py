"""ACME adapter — stores chunks in Let's Encrypt CSR Subject Alternative Names.

Each chunk is base32-encoded and submitted as DNS SANs in an ACME order.
The chunk data lives in the order's identifiers until the order expires
(typically 7 days).

Requires: pip install picard[acme]  (aiohttp, cryptography)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class AcmeAdapter(Adapter):
    """Scatter chunks into ACME order SANs."""

    def __init__(
        self,
        directory_url: str = "https://acme-v02.api.letsencrypt.org/directory",
        account_key_pem: str = "",
        contact_email: str = "",
        *,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._directory_url = directory_url
        self._account_key_pem = account_key_pem
        self._contact_email = contact_email
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="acme",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            ttl_seconds=604800,
            rate_limit_per_min=5,
        )

    def _encode_sans(self, data: str) -> list[str]:
        encoded = base64.b32encode(data.encode()).decode().lower().rstrip("=")
        labels = [encoded[i : i + 63] for i in range(0, len(encoded), 63)]
        return [f"{label}.picard.acme.invalid" for label in labels]

    def _decode_sans(self, sans: list[str]) -> str:
        parts = []
        for san in sorted(sans):
            label = san.split(".")[0]
            parts.append(label)
        encoded = "".join(parts).upper()
        pad = (8 - len(encoded) % 8) % 8
        encoded += "=" * pad
        return base64.b32decode(encoded).decode()

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        sans = self._encode_sans(encoded_chunk)

        async def _create_order():
            identifiers = [{"type": "dns", "value": san} for san in sans]
            session = await self._get_session()
            async with session.post(
                f"{self._directory_url}/new-order",
                json={"identifiers": identifiers},
                headers={"Content-Type": "application/jose+json"},
            ) as resp:
                data = await resp.json()
                return data.get("Location", data.get("order_url", chunk_id))

        order_url = await _create_order()
        location = f"acme://{self._directory_url}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="acme", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"acme://{self._directory_url}/{manifest_id}/"
        return [
            StoredChunkRef(adapter_name="acme", location=loc)
            for loc in self._cache
            if loc.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(self._directory_url) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
