"""Ethereum adapter — stores chunks in smart contract storage.

Each chunk is stored via a key-value storage contract on an EVM chain.
Writes require gas (signed transactions); reads are free (eth_call).
The contract is assumed to be already deployed.

Requires: pip install picard-store[ethereum]   (httpx)
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class EthereumAdapter(Adapter):
    """Scatter chunks into Ethereum contract storage via JSON-RPC."""

    STORE_SELECTOR = "0x1234abcd"
    RETRIEVE_SELECTOR = "0x5678efab"
    REMOVE_SELECTOR = "0x9abc0123"

    def __init__(
        self,
        rpc_url: str,
        contract_address: str,
        private_key: str,
        *,
        chain_id: int = 1,
        max_chunk_bytes: int = 24_000,
    ) -> None:
        self._rpc_url = rpc_url
        self._contract = contract_address
        self._private_key = private_key
        self._chain_id = chain_id
        self._max = max_chunk_bytes
        self._client: object | None = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _key_hash(self, msg_id: str) -> str:
        """Derive a bytes32 storage key from a message ID."""
        h = hashlib.sha256(msg_id.encode()).hexdigest()
        return "0x" + h[:64]

    async def _rpc(self, method: str, params: list) -> dict:
        """Send a JSON-RPC request. Primary mock target."""
        client = await self._ensure_client()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        resp = await client.post(self._rpc_url, json=payload)
        return resp.json()

    async def _send_tx(self, func_selector: str, key: str, data: str = "") -> str:
        """Build, sign (EIP-1559), and submit a transaction. Returns tx hash."""
        from eth_account import Account

        acct = Account.from_key(self._private_key)

        nonce = int(
            (await self._rpc("eth_getTransactionCount", [acct.address, "pending"]))
            .get("result", "0x0"), 16
        )
        gas_price = int(
            (await self._rpc("eth_gasPrice", [])).get("result", "0x3b9aca00"), 16
        )

        calldata = "0x" + bytes.fromhex(func_selector[2:]) + bytes.fromhex(key[2:])
        if data:
            calldata = calldata.hex() if isinstance(calldata, bytes) else calldata
            calldata += data.encode().hex()
        if isinstance(calldata, bytes):
            calldata = "0x" + calldata.hex()

        tx = {
            "chainId": self._chain_id,
            "nonce": nonce,
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": gas_price,
            "gas": 100_000,
            "to": self._contract,
            "value": 0,
            "data": calldata,
        }
        signed = acct.sign_transaction(tx)
        result = await self._rpc(
            "eth_sendRawTransaction",
            ["0x" + signed.raw_transaction.hex()],
        )
        tx_hash = result.get("result", "")
        if not tx_hash:
            raise RuntimeError(f"eth_sendRawTransaction failed: {result}")
        return tx_hash

    async def _eth_call(self, func_selector: str, key: str) -> str:
        """Execute a read-only call against the contract."""
        calldata = func_selector + key[2:]
        result = await self._rpc(
            "eth_call",
            [{"to": self._contract, "data": calldata}, "latest"],
        )
        return result.get("result", "")

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ethereum",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=15,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        key = self._key_hash(msg_id)
        await self._send_tx(self.STORE_SELECTOR, key, encoded_chunk)
        return StoredChunkRef(adapter_name="ethereum", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = self._key_hash(ref.location)
        hex_result = await self._eth_call(self.RETRIEVE_SELECTOR, key)
        if not hex_result or hex_result == "0x":
            raise ValueError(f"no data at key {ref.location}")
        return bytes.fromhex(hex_result.replace("0x", "")).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = self._key_hash(ref.location)
        try:
            await self._send_tx(self.REMOVE_SELECTOR, key)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc("eth_blockNumber", [])
            return "result" in result
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
