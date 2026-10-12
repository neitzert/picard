"""OCSP adapter — stores chunks in OCSP request nonce extensions.

Embeds small chunks in the nonce extension field of OCSP requests.
Ephemeral by nature — uses a local cache for read-back since OCSP
is request/response with no persistent server-side storage of nonces.

Requires: pip install picard[ocsp]  (aiohttp, cryptography)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class OcspAdapter(Adapter):
    """Scatter chunks into OCSP nonce extensions."""

    def __init__(
        self,
        responder_url: str,
        *,
        max_chunk_bytes: int = 128,
    ) -> None:
        self._responder_url = responder_url.rstrip("/")
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
            name="ocsp",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        nonce = encoded_chunk.encode()[:self._max]
        request_hash = hashlib.sha256(nonce).hexdigest()[:16]

        session = await self._get_session()
        async with session.post(
            self._responder_url,
            data=nonce,
            headers={"Content-Type": "application/ocsp-request"},
        ) as resp:
            await resp.read()

        location = f"ocsp://{self._responder_url}/{request_hash}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="ocsp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ocsp", location=loc)
            for loc, data in self._cache.items()
            if manifest_id in loc or manifest_id in data
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(self._responder_url) as resp:
                return resp.status in (200, 405)
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
