"""Bitcoin adapter -- stores chunks in OP_RETURN transactions.

Each chunk becomes an OP_RETURN output (max 80 bytes) on the
Bitcoin blockchain.  Once confirmed, data is permanent and
censorship-resistant.

Requires: pip install picard[bitcoin]  (aiohttp for JSON-RPC)
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk
import aiohttp


class BitcoinAdapter(Adapter):
    """Scatter chunks into Bitcoin OP_RETURN transactions."""

    def __init__(
        self,
        rpc_url: str = "http://127.0.0.1:8332",
        rpc_user: str = "bitcoin",
        rpc_password: str = "",
        *,
        wallet_name: str = "picard",
        max_chunk_bytes: int = 80,
    ) -> None:
        self._rpc_url = rpc_url.rstrip("/")
        self._rpc_user = rpc_user
        self._rpc_password = rpc_password
        self._wallet = wallet_name
        self._max = max_chunk_bytes
        self._session: object | None = None

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        import aiohttp

        self._session = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(self._rpc_user, self._rpc_password),
        )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bitcoin",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def _rpc(self, method: str, params: list | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": "picard",
            "method": method,
            "params": params or [],
        }
        url = f"{self._rpc_url}/wallet/{self._wallet}"
        session = await self._ensure_session()

        async with session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

        if result.get("error"):
            raise RuntimeError(f"RPC error: {result['error']}")
        return result.get("result", {})

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data_hex = encoded_chunk.encode().hex()

        if len(bytes.fromhex(data_hex)) > self._max:
            raise ValueError(f"chunk exceeds {self._max} byte OP_RETURN limit")

        unspent = await self._rpc("listunspent", [1, 9999999])
        if not unspent:
            raise RuntimeError("no UTXOs available for transaction")

        utxo = unspent[0]
        inputs = [{"txid": utxo["txid"], "vout": utxo["vout"]}]

        fee = 0.0001
        change = float(utxo["amount"]) - fee
        change_addr = await self._rpc("getrawchangeaddress")

        outputs = [
            {"data": data_hex},
            {change_addr: round(change, 8)},
        ]

        raw_tx = await self._rpc("createrawtransaction", [inputs, outputs])
        signed = await self._rpc("signrawtransactionwithwallet", [raw_tx])

        if not signed.get("complete"):
            raise RuntimeError("transaction signing failed")

        txid = await self._rpc("sendrawtransaction", [signed["hex"]])

        loc = f"btc://{txid}/0"
        return StoredChunkRef(adapter_name="bitcoin", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix("btc://").split("/")
        txid = parts[0]
        vout = int(parts[1]) if len(parts) > 1 else 0

        raw = await self._rpc("getrawtransaction", [txid, True])
        for output in raw.get("vout", []):
            script = output.get("scriptPubKey", {})
            asm = script.get("asm", "")
            if asm.startswith("OP_RETURN"):
                hex_data = asm.split(" ", 1)[1] if " " in asm else ""
                return bytes.fromhex(hex_data).decode()

        raise KeyError(f"no OP_RETURN found in tx: {txid}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            info = await self._rpc("getblockchaininfo")
            return "chain" in info
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            info = await self._rpc("getwalletinfo")
            balance_sat = int(float(info.get("balance", 0)) * 100_000_000)
            return {
                "used_bytes": None,
                "free_bytes": balance_sat,
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
