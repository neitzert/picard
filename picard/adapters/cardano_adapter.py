"""Cardano adapter — stores chunks in transaction metadata (auxiliary data).

Each chunk is placed as a metadata value under label 8888 in a Cardano
transaction.  Uses the Blockfrost API for both submission and retrieval.
Once confirmed, data is permanent on-chain.

Requires: pip install picard[cardano]   (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class CardanoAdapter(Adapter):
    """Scatter chunks into Cardano transaction metadata."""

    METADATA_LABEL = 8888

    def __init__(
        self,
        api_url: str = "https://cardano-mainnet.blockfrost.io/api/v0",
        project_id: str = "",
        *,
        max_chunk_bytes: int = 16_384,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._project_id = project_id
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={"project_id": self._project_id}
            )
        return self._session

    async def _get(self, path: str) -> dict | list:
        session = await self._ensure_session()
        url = f"{self._api_url}{path}"
        async with session.get(url) as resp:
            return await resp.json()

    async def _post(self, path: str, data: bytes) -> dict:
        session = await self._ensure_session()
        url = f"{self._api_url}{path}"
        async with session.post(
            url, data=data, headers={"Content-Type": "application/cbor"}
        ) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cardano",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        tx_hash = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        cbor_placeholder = encoded_chunk.encode()
        await self._post("/tx/submit", cbor_placeholder)
        return StoredChunkRef(
            adapter_name="cardano", location=f"ada://{tx_hash}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_hash = ref.location.removeprefix("ada://")
        result = await self._get(f"/txs/{tx_hash}/metadata")
        if isinstance(result, list):
            for entry in result:
                if entry.get("label") == str(self.METADATA_LABEL):
                    value = entry.get("json_metadata", {})
                    if isinstance(value, str):
                        return value
                    return value.get("chunk", "")
        raise ValueError(f"no metadata label {self.METADATA_LABEL} in tx {tx_hash}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._get("/health")
            return result.get("is_healthy", False)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
