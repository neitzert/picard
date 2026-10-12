"""Solana adapter — stores chunks as Memo program instructions.

Each chunk is embedded in a Memo instruction within a legacy transaction.
Data is permanent and on-chain once confirmed. Reads are free via
getTransaction; writes cost SOL for gas.

Requires: pip install picard-store[solana]   (aiohttp, cryptography)
"""

from __future__ import annotations

import base64
import json
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Solana serialisation helpers (no external deps)
# ---------------------------------------------------------------------------

_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHA.index(c)
    size = (n.bit_length() + 7) // 8 if n else 0
    pad = sum(1 for c in s if c == "1")
    return b"\x00" * pad + (n.to_bytes(size, "big") if size else b"")


def _uleb128(n: int) -> bytes:
    """Encode n as Solana compact-u16 (ULEB-128)."""
    out = []
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)


def _build_memo_tx(payer_pubkey: bytes, memo_data: bytes, blockhash: bytes) -> bytes:
    """Build an unsigned Solana legacy transaction message for a Memo instruction."""
    memo_program = _b58decode("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    # Message header: 1 req_sig, 0 readonly_signed, 1 readonly_unsigned
    header = bytes([1, 0, 1])
    # Account keys: [payer, MEMO_PROGRAM_ID]
    accounts = _uleb128(2) + payer_pubkey + memo_program
    # One instruction: program_idx=1, 0 account indices, memo_data
    instruction = bytes([1]) + _uleb128(0) + _uleb128(len(memo_data)) + memo_data
    return header + accounts + blockhash + _uleb128(1) + instruction


def _sign_tx(seed: bytes, message: bytes) -> bytes:
    """Sign *message* with Ed25519 *seed*, return full signed transaction bytes."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    privkey = Ed25519PrivateKey.from_private_bytes(seed)
    sig = privkey.sign(message)
    pubkey = privkey.public_key().public_bytes_raw()
    return _uleb128(1) + sig + message, pubkey


def _load_seed(keypair_path: str | None, keypair_hex: str) -> bytes:
    """Return 32-byte Ed25519 seed from a JSON keypair file or hex string."""
    if keypair_hex:
        return bytes.fromhex(keypair_hex)[:32]
    if keypair_path:
        with open(keypair_path) as fh:
            raw = json.load(fh)
        return bytes(raw[:32])
    raise ValueError("SolanaAdapter: keypair_path or keypair_hex is required")


class SolanaAdapter(Adapter):
    """Scatter chunks into Solana Memo instructions."""

    MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

    def __init__(
        self,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        keypair_path: str | None = None,
        *,
        keypair_hex: str = "",
        program_id: str | None = None,
        max_chunk_bytes: int = 566,
    ) -> None:
        self._rpc_url = rpc_url
        self._keypair_path = keypair_path
        self._keypair_hex = keypair_hex
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
        seed = _load_seed(self._keypair_path, self._keypair_hex)

        # Fetch recent blockhash
        bh_result = await self._rpc(
            "getLatestBlockhash", [{"commitment": "finalized"}]
        )
        blockhash_b58 = bh_result["result"]["value"]["blockhash"]
        blockhash = _b58decode(blockhash_b58)

        # Build message
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        privkey = Ed25519PrivateKey.from_private_bytes(seed)
        payer_pubkey = privkey.public_key().public_bytes_raw()
        memo_data = encoded_chunk.encode()
        message = _build_memo_tx(payer_pubkey, memo_data, blockhash)

        # Sign
        sig = privkey.sign(message)
        signed_tx = _uleb128(1) + sig + message
        tx_b64 = base64.b64encode(signed_tx).decode()

        result = await self._rpc(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": False}],
        )
        sig_str = result.get("result")
        if not sig_str:
            raise RuntimeError(f"Solana sendTransaction failed: {result}")

        return StoredChunkRef(adapter_name="solana", location=f"sol://{sig_str}")

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
                data = ix.get("parsed", {})
                if isinstance(data, str):
                    return base64.b64decode(data).decode()
                # parsed memo returns {"type": "...", "info": {"memo": "..."}}
                memo = ix.get("parsed", {}).get("info", {}).get("memo", "")
                if memo:
                    return memo
                raw = ix.get("data", "")
                if raw:
                    return base64.b64decode(raw).decode()
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
