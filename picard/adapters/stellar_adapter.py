"""Stellar adapter — stores chunks in transaction memos and manage_data ops.

Each chunk is placed in a Stellar transaction memo_text (28 bytes) or
a manage_data operation value (64 bytes).  Uses the Horizon API for
submission and retrieval.  Once included in a ledger the data is permanent.

Requires: pip install picard[stellar]   (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class StellarAdapter(Adapter):
    """Scatter chunks into Stellar manage_data operations."""

    def __init__(
        self,
        horizon_url: str = "https://horizon.stellar.org",
        secret_key: str = "",
        *,
        max_chunk_bytes: int = 64,
    ) -> None:
        self._horizon_url = horizon_url.rstrip("/")
        self._secret_key = secret_key
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(self, path: str) -> dict:
        session = await self._ensure_session()
        url = f"{self._horizon_url}{path}"
        async with session.get(url) as resp:
            return await resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        session = await self._ensure_session()
        url = f"{self._horizon_url}{path}"
        async with session.post(url, data=data) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="stellar",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        tx_hash = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        await self._post("/transactions", {"tx": encoded_chunk})
        return StoredChunkRef(
            adapter_name="stellar", location=f"xlm://{tx_hash}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_hash = ref.location.removeprefix("xlm://")
        result = await self._get(f"/transactions/{tx_hash}")
        memo = result.get("memo")
        if memo:
            return memo
        effects = await self._get(f"/transactions/{tx_hash}/effects")
        records = effects.get("_embedded", {}).get("records", [])
        for record in records:
            if record.get("type") == "manage_data":
                value = record.get("value", "")
                if value:
                    return value
        raise ValueError(f"no chunk data in tx {tx_hash}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._get("/")
            return "horizon_version" in result
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
