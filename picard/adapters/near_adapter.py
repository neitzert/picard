"""NEAR Protocol adapter — stores chunks in function call args on a storage contract.

Each chunk is sent as arguments to a FunctionCall action.  Reads use a
view call (free, no signing) to query state.  Once included in a block
the data is permanent.

Requires: pip install picard[near]   (aiohttp, cryptography)
"""

from __future__ import annotations

import base64
import json
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# NEAR borsh serialisation + Ed25519 helpers (no external deps)
# ---------------------------------------------------------------------------

_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHA.index(c)
    size = (n.bit_length() + 7) // 8 if n else 0
    pad = sum(1 for c in s if c == "1")
    return b"\x00" * pad + (n.to_bytes(size, "big") if size else b"")


def _borsh_str(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(4, "little") + b


def _borsh_bytes(b: bytes) -> bytes:
    return len(b).to_bytes(4, "little") + b


def _build_near_tx(
    signer_id: str,
    pubkey_bytes: bytes,
    nonce: int,
    receiver_id: str,
    block_hash_bytes: bytes,
    method_name: str,
    args_json: str,
    gas: int = 30_000_000_000_000,
    deposit: int = 0,
) -> bytes:
    """Return the borsh-serialised NEAR Transaction (unsigned)."""
    args_bytes = args_json.encode()
    action = (
        b"\x02"                          # FunctionCall tag
        + _borsh_str(method_name)
        + _borsh_bytes(args_bytes)
        + gas.to_bytes(8, "little")
        + deposit.to_bytes(16, "little")
    )
    return (
        _borsh_str(signer_id)
        + b"\x00" + pubkey_bytes          # key_type=0 (Ed25519) + 32-byte key
        + nonce.to_bytes(8, "little")
        + _borsh_str(receiver_id)
        + block_hash_bytes
        + b"\x01\x00\x00\x00"            # 1 action
        + action
    )


def _sign_near_tx(tx_bytes: bytes, privkey_hex: str) -> bytes:
    """Return borsh-serialised SignedTransaction."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import hashlib
    seed = bytes.fromhex(privkey_hex)[:32]
    privkey = Ed25519PrivateKey.from_private_bytes(seed)
    # NEAR signs the SHA-256 hash of the serialised transaction
    tx_hash = hashlib.sha256(tx_bytes).digest()
    sig = privkey.sign(tx_hash)
    # SignedTransaction = Transaction || key_type (1B) || signature (64B)
    return tx_bytes + b"\x00" + sig


class NearAdapter(Adapter):
    """Scatter chunks into NEAR Protocol contract state."""

    def __init__(
        self,
        rpc_url: str = "https://rpc.mainnet.near.org",
        account_id: str = "",
        private_key: str = "",
        *,
        contract_id: str = "",
        max_chunk_bytes: int = 4_194_304,
    ) -> None:
        self._rpc_url = rpc_url
        self._account_id = account_id
        self._private_key = private_key          # hex or "ed25519:<base58>"
        self._contract_id = contract_id or account_id
        self._max = max_chunk_bytes
        self._session = None

    def _key_seed(self) -> str:
        """Return private key as 64-char hex string."""
        pk = self._private_key
        if pk.startswith("ed25519:"):
            raw = _b58decode(pk[len("ed25519:"):])
            return raw[:32].hex()
        if pk.startswith("0x"):
            return pk[2:]
        return pk  # assume already hex

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _rpc(self, method: str, params: dict | list) -> dict:
        session = await self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with session.post(self._rpc_url, json=payload) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="near",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def _build_and_submit(self, method_name: str, args_json: str) -> dict:
        """Sign and submit a FunctionCall transaction. Returns RPC result."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed_hex = self._key_seed()
        seed = bytes.fromhex(seed_hex)
        privkey = Ed25519PrivateKey.from_private_bytes(seed)
        pubkey_bytes = privkey.public_key().public_bytes_raw()

        # Derive public key in NEAR "ed25519:<base58>" format for access key query
        pk_b58 = "ed25519:" + _b58_encode_raw(pubkey_bytes)

        # Fetch nonce and recent block hash
        ak_result = await self._rpc("query", {
            "request_type": "view_access_key",
            "finality": "final",
            "account_id": self._account_id,
            "public_key": pk_b58,
        })
        nonce = ak_result["result"]["nonce"] + 1

        block_result = await self._rpc("block", {"finality": "final"})
        block_hash_b58 = block_result["result"]["header"]["hash"]
        block_hash_bytes = _b58decode(block_hash_b58)

        tx_bytes = _build_near_tx(
            self._account_id, pubkey_bytes, nonce,
            self._contract_id, block_hash_bytes, method_name, args_json,
        )
        signed_tx = _sign_near_tx(tx_bytes, seed_hex)
        tx_b64 = base64.b64encode(signed_tx).decode()

        return await self._rpc("broadcast_tx_async", [tx_b64])

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import uuid
        chunk_key = uuid.uuid4().hex
        args = json.dumps({"key": chunk_key, "data": encoded_chunk})
        await self._build_and_submit("store_chunk", args)
        return StoredChunkRef(adapter_name="near", location=f"near://{chunk_key}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_key = ref.location.removeprefix("near://")
        args = base64.b64encode(json.dumps({"key": chunk_key}).encode()).decode()
        result = await self._rpc("query", {
            "request_type": "call_function",
            "finality": "final",
            "account_id": self._contract_id,
            "method_name": "get_chunk",
            "args_base64": args,
        })
        res = result.get("result", {})
        raw = res.get("result", [])
        if not raw:
            raise ValueError(f"no chunk data for key {chunk_key}")
        decoded = bytes(raw).decode()
        payload = json.loads(decoded)
        return payload.get("data", decoded)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_key = ref.location.removeprefix("near://")
        args = json.dumps({"key": chunk_key})
        try:
            await self._build_and_submit("delete_chunk", args)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc("status", [])
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


def _b58_encode_raw(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58_ALPHA[r] + s
    pad = sum(1 for b in data if b == 0)
    return "1" * pad + s
