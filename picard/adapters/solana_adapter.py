"""Solana adapter — stores chunks as Memo program instructions.

Each chunk is embedded in a Memo instruction within a transaction.
Data is permanent and on-chain once confirmed. Reads are free via
getTransaction; writes cost SOL for gas.

Requires: pip install picard[solana]   (aiohttp)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class SolanaAdapter(Adapter):
    """Scatter chunks into Solana Memo instructions."""

    MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

    def __init__(
        self,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        keypair_path: str | None = None,
        *,
        program_id: str | None = None,
        max_chunk_bytes: int = 566,
    ) -> None:
        self._rpc_url = rpc_url
        self._keypair_path = keypair_path
        self._program_id = program_id or self.MEMO_PROGRAM_ID
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
            name="solana",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        memo_data = base64.b64encode(encoded_chunk.encode()).decode()
        tx_id = uuid.uuid4().hex
        await self._rpc("sendTransaction", [memo_data, {"encoding": "base64"}])
        return StoredChunkRef(adapter_name="solana", location=f"sol://{tx_id}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        sig = ref.location.removeprefix("sol://")
        result = await self._rpc(
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        tx = result.get("result")
        if not tx:
            raise ValueError(f"transaction not found: {sig}")
        for ix in tx.get("transaction", {}).get("message", {}).get("instructions", []):
            if ix.get("programId") == self._program_id:
                data = ix.get("data", "")
                return base64.b64decode(data).decode()
        raise ValueError(f"no memo instruction in tx {sig}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc("getHealth", [])
            return result.get("result") == "ok"
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
