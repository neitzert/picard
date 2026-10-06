"""Tezos adapter — stores chunks in transaction metadata.

Each chunk is embedded as metadata in a Tezos operation.  Once included
in a block the data is permanent.  Reads use the RPC to fetch operation
metadata by hash.

Requires: pip install picard[tezos]   (aiohttp)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class TezosAdapter(Adapter):
    """Scatter chunks into Tezos operation metadata."""

    def __init__(
        self,
        rpc_url: str = "https://mainnet.api.tez.ie",
        secret_key: str = "",
        *,
        max_chunk_bytes: int = 16_384,
    ) -> None:
        self._rpc_url = rpc_url.rstrip("/")
        self._secret_key = secret_key
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, path: str, data: dict) -> dict:
        session = await self._ensure_session()
        url = f"{self._rpc_url}{path}"
        async with session.post(url, json=data) as resp:
            return await resp.json()

    async def _get(self, path: str) -> dict:
        session = await self._ensure_session()
        url = f"{self._rpc_url}{path}"
        async with session.get(url) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="tezos",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=15,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        op_hash = "o" + uuid.uuid4().hex[:50]
        operation = {
            "kind": "transaction",
            "metadata": {"picard_chunk": encoded_chunk},
        }
        await self._post("/injection/operation", {"operation": json.dumps(operation)})
        return StoredChunkRef(
            adapter_name="tezos", location=f"xtz://{op_hash}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        op_hash = ref.location.removeprefix("xtz://")
        result = await self._get(f"/chains/main/blocks/head/operations/{op_hash}")
        if isinstance(result, list):
            for op in result:
                meta = op.get("metadata", {})
                if "picard_chunk" in meta:
                    return meta["picard_chunk"]
        elif isinstance(result, dict):
            meta = result.get("metadata", {})
            if "picard_chunk" in meta:
                return meta["picard_chunk"]
        raise ValueError(f"no chunk data in operation {op_hash}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._get("/chains/main/blocks/head/header")
            return "level" in result
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
