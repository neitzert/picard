"""Mempool adapter -- stores chunks as pending Ethereum transactions.

Ethereum's transaction pool (mempool) has a "queued" layer for transactions
whose nonce is ahead of the sender's current confirmed nonce. A transaction
with nonce = current_confirmed_nonce + 1000 cannot be mined until the 999
preceding nonces are sent. It sits in the queued pool indefinitely.

This is the nonce-gap trick: by sending a transaction far ahead of the
account's confirmed nonce, the chunk lives in the node's in-memory pool
as a pending-but-unmined transaction. The chunk bytes are stored in the
transaction's `data` (input) field.

Lifetime is controlled by the Ethereum node, not by the transaction fee.
The default geth eviction time for queued transactions is 3 hours. To
extend this, run your own geth node with:

    geth ... \\
      --txpool.lifetime 438000h \\
      --txpool.journal /var/lib/geth/txpool.journal \\
      --txpool.accountqueue 256 \\
      --txpool.globalqueue 1024

Setting maxFeePerGas=0 and maxPriorityFeePerGas=0 (EIP-1559 type-2 tx)
ensures the transaction will never be mined even if the nonce gap closes
accidentally -- no validator will include a zero-fee transaction.

The chunk is retrieved via eth_getTransactionByHash. The transaction's
`input` field carries the raw chunk bytes, encoded as UTF-8.

Deletion is done via RBF (Replace-By-Fee): send a replacement transaction
at the same nonce with empty data and a small non-zero fee. The node evicts
the original zero-fee transaction in favour of the replacement.

EXPERIMENTAL: Run at your own risk. This adapter uses undocumented node
memory-pool behaviour that varies between Ethereum client implementations
(geth, erigon, nethermind). Results depend on the node's txpool configuration.
Data is NOT on-chain -- it lives in the node's RAM and is lost if the node
restarts without a txpool journal, or if the node's eviction policy runs.

WARNING: Use only with a node you control. Broadcasting zero-fee transactions
to mainnet may be rejected by public nodes. Use a local geth --dev instance
or a testnet (Sepolia) for development. Never use this adapter as the sole
storage backend for critical data.

Requires: pip install web3
"""

from __future__ import annotations

import json
import logging
import warnings
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

log = logging.getLogger(__name__)

try:
    from web3 import Web3
    from web3.exceptions import TransactionNotFound
    _WEB3_AVAILABLE = True
except ImportError:
    _WEB3_AVAILABLE = False


def _require_web3() -> None:
    if not _WEB3_AVAILABLE:
        raise ImportError(
            "web3 is required for the mempool adapter. "
            "Install it with: pip install web3"
        )


# Minimum replacement fee to evict a zero-fee queued transaction via RBF.
_RBF_GAS_PRICE = 1_000_000_000  # 1 gwei


class MempoolAdapter(Adapter):
    """Ethereum mempool adapter using the nonce-gap trick.

    Each write allocates the next slot index and sends a transaction with
    nonce = confirmed_nonce + nonce_gap + slot_index. The transaction is
    never mined (zero fee, nonce gap), so it stays in the queued pool for
    as long as the node's --txpool.lifetime allows.

    Parameters
    ----------
    node_url:
        HTTP/WS URL of the Ethereum node. Defaults to local geth --dev.
    private_key:
        Hex-encoded private key for signing. If None, a fresh key is
        generated automatically -- save it or the stored data is unreadable.
    nonce_gap:
        How far ahead of the current confirmed nonce to place transactions.
        Larger values reduce the risk of the gap closing accidentally.
    network:
        Informational label. Does not affect behaviour.
    tip_lifetime:
        Human-readable desired lifetime. The adapter logs the corresponding
        geth --txpool.lifetime flag value at startup.
    max_chunk_bytes:
        Maximum chunk size the adapter accepts.
    """

    _ADAPTER_NAME = "mempool"

    def __init__(
        self,
        node_url: str = "http://127.0.0.1:8545",
        private_key: str | None = None,
        nonce_gap: int = 1000,
        network: str = "localhost",
        tip_lifetime: str = "3h",
        max_chunk_bytes: int = 65536,
    ) -> None:
        _require_web3()

        self._node_url = node_url
        self._nonce_gap = nonce_gap
        self._network = network
        self._tip_lifetime = tip_lifetime
        self._max_chunk_bytes = max_chunk_bytes

        self._w3 = Web3(Web3.HTTPProvider(node_url))

        if private_key is None:
            acct = Web3().eth.account.create()
            self._account = acct
            warnings.warn(
                f"MempoolAdapter: no private_key supplied. "
                f"Generated a fresh key for address {acct.address}. "
                f"Save this key or stored chunks will be unreadable: "
                f"{acct.key.hex()}",
                stacklevel=2,
            )
        else:
            pk = private_key if private_key.startswith("0x") else "0x" + private_key
            self._account = Web3().eth.account.from_key(pk)

        # slot_index advances with each write; nonce = confirmed + gap + slot
        self._slot_index: int = 0

        # txhash -> {"manifest_id": str, "nonce": int, "data_length": int}
        self._registry: dict[str, dict] = {}

        self._log_lifetime_hint()

    # ------------------------------------------------------------------
    # Adapter ABC
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=self._ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=False,   # mempool is in-memory; node restart can lose it
            ordered=False,
            rate_limit_per_min=20,
            tags={
                "tier": "4",
                "experimental": "true",
                "substrate": "ethereum-mempool",
                "network": self._network,
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        """Sign and broadcast a zero-fee queued transaction carrying the chunk."""
        data_bytes = encoded_chunk.encode("utf-8")
        if len(data_bytes) > self._max_chunk_bytes:
            raise ValueError(
                f"Chunk size {len(data_bytes)} exceeds max {self._max_chunk_bytes}"
            )

        confirmed_nonce = self._w3.eth.get_transaction_count(
            self._account.address, "latest"
        )
        tx_nonce = confirmed_nonce + self._nonce_gap + self._slot_index

        chain_id = self._w3.eth.chain_id
        tx = {
            "type": 2,                      # EIP-1559
            "chainId": chain_id,
            "nonce": tx_nonce,
            "to": self._account.address,    # self-send; no ETH moves
            "value": 0,
            "gas": 21000 + 16 * len(data_bytes),  # base + data cost estimate
            "maxFeePerGas": 0,
            "maxPriorityFeePerGas": 0,
            "data": data_bytes,
        }

        signed = self._w3.eth.account.sign_transaction(tx, self._account.key)
        txhash = self._w3.eth.send_raw_transaction(signed.rawTransaction).hex()
        if not txhash.startswith("0x"):
            txhash = "0x" + txhash

        self._registry[txhash] = {
            "nonce": tx_nonce,
            "data_length": len(data_bytes),
            "manifest_id": None,
        }
        self._slot_index += 1

        log.debug("mempool write: txhash=%s nonce=%d", txhash, tx_nonce)

        location = json.dumps({
            "txhash": txhash,
            "nonce": tx_nonce,
            "data_length": len(data_bytes),
        })
        return StoredChunkRef(adapter_name=self._ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        """Retrieve chunk bytes from a pending transaction's input field."""
        loc = json.loads(ref.location)
        txhash = loc["txhash"]

        try:
            tx = self._w3.eth.get_transaction(txhash)
        except TransactionNotFound:
            raise KeyError(f"Transaction {txhash} not found in mempool") from None

        if tx is None:
            raise KeyError(f"Transaction {txhash} not found in mempool")

        raw = tx.get("input") or tx.get("data") or b""
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8")
        # HexBytes or hex string
        if hasattr(raw, "hex"):
            raw = raw  # HexBytes -- decode directly
            return bytes(raw).decode("utf-8")
        return bytes.fromhex(str(raw).removeprefix("0x")).decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        """Evict the queued transaction via RBF with a small non-zero fee."""
        loc = json.loads(ref.location)
        txhash = loc["txhash"]
        nonce = loc["nonce"]

        chain_id = self._w3.eth.chain_id
        tx = {
            "type": 2,
            "chainId": chain_id,
            "nonce": nonce,
            "to": self._account.address,
            "value": 0,
            "gas": 21000,
            "maxFeePerGas": _RBF_GAS_PRICE,
            "maxPriorityFeePerGas": _RBF_GAS_PRICE,
            "data": b"",
        }
        signed = self._w3.eth.account.sign_transaction(tx, self._account.key)
        try:
            self._w3.eth.send_raw_transaction(signed.rawTransaction)
        except Exception as exc:
            log.warning("mempool delete RBF failed for %s: %s", txhash, exc)
            return False

        self._registry.pop(txhash, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """Return refs for all chunks tagged with manifest_id in this session."""
        results = []
        for txhash, meta in self._registry.items():
            if meta.get("manifest_id") == manifest_id:
                location = json.dumps({
                    "txhash": txhash,
                    "nonce": meta["nonce"],
                    "data_length": meta["data_length"],
                })
                results.append(
                    StoredChunkRef(adapter_name=self._ADAPTER_NAME, location=location)
                )
        return results

    async def health_check(self) -> bool:
        """Return True if the Ethereum node is reachable."""
        try:
            _ = self._w3.eth.chain_id
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_lifetime_hint(self) -> None:
        """Log the geth flags needed for the requested tip_lifetime."""
        lifetime_map = {
            "3h": "3h",
            "1d": "24h",
            "30d": "720h",
            "1y": "8760h",
            "10y": "87600h",
            "50y": "438000h",
        }
        geth_val = lifetime_map.get(self._tip_lifetime, self._tip_lifetime)
        log.info(
            "MempoolAdapter: for %s lifetime, run geth with "
            "--txpool.lifetime %s --txpool.journal /var/lib/geth/txpool.journal",
            self._tip_lifetime,
            geth_val,
        )
