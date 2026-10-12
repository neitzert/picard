"""Tezos adapter — stores chunks in originated contract storage.

Each chunk is stored as the initial storage of a minimal origination:
  parameter unit ; storage string ; code { CDR ; NIL operation ; PAIR }

Once included in a block the data is permanent.

write_chunk  — originates a contract; location = xtz://{op_hash}
read_chunk   — derives KT1 address via TzKT indexer, reads storage via node RPC
delete_chunk → False (immutable on-chain)
list_chunks  → [] (no on-chain enumeration)

Requires: pip install picard-store[tezos]   (aiohttp, cryptography)
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Tezos key utilities (base58check, tz1 address derivation)
# ---------------------------------------------------------------------------

_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TZ1_PREFIX = bytes([6, 161, 159])
_EDSK_PREFIX = bytes([43, 246, 78, 7])       # 64-byte expanded edsk
_EDSK_SEED_PREFIX = bytes([13, 15, 58, 7])   # 32-byte seed edsk


def _b58decode_check(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHA.index(c)
    size = (n.bit_length() + 7) // 8 if n else 0
    pad = sum(1 for c in s if c == "1")
    raw = b"\x00" * pad + (n.to_bytes(size, "big") if size else b"")
    payload, checksum = raw[:-4], raw[-4:]
    calc = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != calc:
        raise ValueError("bad base58check checksum")
    return payload


def _b58encode_check(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + checksum
    n = int.from_bytes(data, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58_ALPHA[r] + s
    pad = sum(1 for b in data if b == 0)
    return "1" * pad + s


def _tezos_seed(secret_key: str) -> bytes:
    """Return 32-byte Ed25519 seed from hex or Tezos edsk key string."""
    if secret_key.startswith(("0x", "0X")):
        return bytes.fromhex(secret_key[2:])[:32]
    payload = _b58decode_check(secret_key)
    if payload[:4] in (_EDSK_PREFIX, _EDSK_SEED_PREFIX):
        return payload[4:36]
    raise ValueError(f"unrecognised Tezos key prefix: {payload[:4].hex()}")


def _tz1_from_seed(seed: bytes) -> str:
    """Derive tz1 Tezos address from an Ed25519 seed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pk = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    pkh = hashlib.blake2b(pk, digest_size=20).digest()
    return _b58encode_check(_TZ1_PREFIX + pkh)


# Minimal Michelson contract: parameter unit; storage string; code { CDR; NIL operation; PAIR }
_CONTRACT_CODE = [
    {"prim": "parameter", "args": [{"prim": "unit"}]},
    {"prim": "storage",   "args": [{"prim": "string"}]},
    {"prim": "code",      "args": [[
        {"prim": "CDR"},
        {"prim": "NIL",  "args": [{"prim": "operation"}]},
        {"prim": "PAIR"},
    ]]},
]


class TezosAdapter(Adapter):
    """Scatter chunks into Tezos contract storage via origination."""

    def __init__(
        self,
        rpc_url: str = "https://mainnet.api.tez.ie",
        secret_key: str = "",
        *,
        tzkt_url: str = "https://api.tzkt.io",
        max_chunk_bytes: int = 16_384,
    ) -> None:
        self._rpc_url = rpc_url.rstrip("/")
        self._secret_key = secret_key
        self._tzkt_url = tzkt_url.rstrip("/")
        self._max = max_chunk_bytes
        self._session = None
        self._account_id: str | None = None

    def _ensure_account_id(self) -> str:
        if self._account_id is None:
            self._account_id = _tz1_from_seed(_tezos_seed(self._secret_key))
        return self._account_id

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _rpc_get(self, path: str) -> object:
        session = await self._ensure_session()
        async with session.get(f"{self._rpc_url}{path}") as resp:
            return await resp.json()

    async def _rpc_post(self, path: str, data: object) -> object:
        session = await self._ensure_session()
        if isinstance(data, (dict, list)):
            async with session.post(f"{self._rpc_url}{path}", json=data) as resp:
                return await resp.json()
        body = json.dumps(data)
        async with session.post(
            f"{self._rpc_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
        ) as resp:
            return await resp.json()

    async def _tzkt_get(self, path: str) -> object:
        session = await self._ensure_session()
        async with session.get(f"{self._tzkt_url}{path}") as resp:
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
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed = _tezos_seed(self._secret_key)
        source = self._ensure_account_id()

        counter_raw = await self._rpc_get(
            f"/chains/main/blocks/head/context/contracts/{source}/counter"
        )
        counter = int(counter_raw) + 1
        storage_limit = str(len(encoded_chunk.encode()) + 350)

        head = await self._rpc_get("/chains/main/blocks/head/hash")
        operation = {
            "branch": head,
            "contents": [{
                "kind": "origination",
                "source": source,
                "fee": "2500",
                "counter": str(counter),
                "gas_limit": "10307",
                "storage_limit": storage_limit,
                "balance": "0",
                "script": {
                    "code": _CONTRACT_CODE,
                    "storage": {"string": encoded_chunk},
                },
            }],
        }

        forged_hex = await self._rpc_post(
            "/chains/main/blocks/head/helpers/forge/operations", operation
        )
        # Sign: watermark byte 0x03 + forged bytes
        privkey = Ed25519PrivateKey.from_private_bytes(seed)
        sig = privkey.sign(b"\x03" + bytes.fromhex(forged_hex))
        signed_hex = forged_hex + sig.hex()

        op_hash = await self._rpc_post("/injection/operation", signed_hex)
        return StoredChunkRef(adapter_name="tezos", location=f"xtz://{op_hash}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        op_hash = ref.location.removeprefix("xtz://")
        # TzKT indexer provides the originated contract address
        results = await self._tzkt_get(f"/v1/operations/originations/{op_hash}")
        if not results:
            raise ValueError(f"origination {op_hash} not found in TzKT indexer")
        kt1 = results[0].get("originatedContract", {}).get("address")
        if not kt1:
            raise ValueError(f"no originated contract for operation {op_hash}")
        storage = await self._rpc_get(
            f"/chains/main/blocks/head/context/contracts/{kt1}/storage"
        )
        if isinstance(storage, dict) and "string" in storage:
            return storage["string"]
        raise ValueError(f"unexpected storage at {kt1}: {storage!r}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc_get("/chains/main/blocks/head/header")
            return isinstance(result, dict) and "level" in result
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
