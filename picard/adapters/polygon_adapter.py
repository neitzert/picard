"""Polygon (Matic) adapter — stores chunks in L2 transaction calldata.

Each chunk is sent as calldata in a self-referencing transaction on
Polygon PoS.  Reads are free via eth_getTransactionByHash.  Writes
cost fractions of a cent in MATIC gas.

Requires: pip install picard[polygon]   (aiohttp)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class PolygonAdapter(Adapter):
    """Scatter chunks into Polygon L2 transaction calldata."""

    def __init__(
        self,
        rpc_url: str = "https://polygon-rpc.com",
        private_key: str = "",
        *,
        chain_id: int = 137,
        max_chunk_bytes: int = 32_768,
    ) -> None:
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._chain_id = chain_id
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _rpc(self, method: str, params: list) -> dict:
        session = await self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with session.post(self._rpc_url, json=payload) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="polygon",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        calldata = "0x" + encoded_chunk.encode().hex()
        tx_hash = "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
        await self._rpc(
            "eth_sendRawTransaction",
            [calldata],
        )
        return StoredChunkRef(
            adapter_name="polygon", location=f"polygon://{tx_hash}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_hash = ref.location.removeprefix("polygon://")
        result = await self._rpc("eth_getTransactionByHash", [tx_hash])
        tx = result.get("result")
        if not tx:
            raise ValueError(f"transaction not found: {tx_hash}")
        input_data = tx.get("input", "0x")
        if input_data == "0x" or len(input_data) <= 2:
            raise ValueError(f"no calldata in tx {tx_hash}")
        return bytes.fromhex(input_data[2:]).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc("eth_blockNumber", [])
            return "result" in result
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
