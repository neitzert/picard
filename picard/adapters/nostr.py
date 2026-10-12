"""Nostr adapter — stores chunks as kind-1 text note events on relays.

Each chunk becomes a NIP-01 event published to the configured relay.
Events are tagged with ["picard", manifest_id] for discovery.
Signing is delegated to a _sign_event method for pluggable backends.

Requires: pip install picard-store[nostr]
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

# ---------------------------------------------------------------------------
# secp256k1 + BIP-340 Schnorr (stdlib only: hashlib integers)
# ---------------------------------------------------------------------------

_P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _ec_add(P, Q):
    if P is None: return Q
    if Q is None: return P
    px, py = P; qx, qy = Q
    if px == qx:
        if py != qy: return None
        lam = (3 * px * px * pow(2 * py, _P - 2, _P)) % _P
    else:
        lam = ((qy - py) * pow(qx - px, _P - 2, _P)) % _P
    x = (lam * lam - px - qx) % _P
    return (x, (lam * (px - x) - py) % _P)


def _ec_mul(k, P=None):
    if P is None: P = (_Gx, _Gy)
    R = None
    while k > 0:
        if k & 1: R = _ec_add(R, P)
        P = _ec_add(P, P)
        k >>= 1
    return R


def _tagged_hash(tag: str, data: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + data).digest()


def _schnorr_pubkey(privhex: str) -> str:
    """Return the 32-byte x-only BIP-340 public key as hex."""
    d = int(privhex, 16) % _N
    return _ec_mul(d)[0].to_bytes(32, "big").hex()


def _schnorr_sign(privhex: str, event_id_hex: str) -> str:
    """Return a 64-byte BIP-340 Schnorr signature as hex."""
    d0 = int(privhex, 16) % _N
    msg = bytes.fromhex(event_id_hex)
    P = _ec_mul(d0)
    d = d0 if P[1] % 2 == 0 else _N - d0
    aux = _tagged_hash("BIP0340/aux", b"\x00" * 32)
    t = (d ^ int.from_bytes(aux, "big")).to_bytes(32, "big")
    k0 = int.from_bytes(
        _tagged_hash("BIP0340/nonce", t + P[0].to_bytes(32, "big") + msg), "big"
    ) % _N
    if k0 == 0:
        raise ValueError("degenerate nonce — choose a different private key")
    R = _ec_mul(k0)
    k = k0 if R[1] % 2 == 0 else _N - k0
    e = int.from_bytes(
        _tagged_hash(
            "BIP0340/challenge",
            R[0].to_bytes(32, "big") + P[0].to_bytes(32, "big") + msg,
        ), "big",
    ) % _N
    return (R[0].to_bytes(32, "big") + ((k + e * d) % _N).to_bytes(32, "big")).hex()


def _compute_event_id(event: dict) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"],
         event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class NostrAdapter(Adapter):
    """Scatter chunks into Nostr relay events via WebSocket (NIP-01)."""

    def __init__(
        self,
        relay_url: str,
        private_key_hex: str,
        *,
        max_chunk_bytes: int = 8000,
    ) -> None:
        self._relay_url = relay_url.rstrip("/")
        self._privkey_hex = private_key_hex
        self._pubkey_hex: str | None = None
        self._max = max_chunk_bytes

    def _get_pubkey(self) -> str:
        if self._pubkey_hex is None:
            self._pubkey_hex = self._derive_pubkey(self._privkey_hex)
        return self._pubkey_hex

    def _derive_pubkey(self, privkey_hex: str) -> str:
        """Return the x-only BIP-340 public key for *privkey_hex*."""
        return _schnorr_pubkey(privkey_hex)

    def _sign_event(self, event: dict) -> str:
        """Return a BIP-340 Schnorr signature over the event id."""
        return _schnorr_sign(self._privkey_hex, event["id"])

    def _build_event(self, content: str, kind: int = 1,
                     tags: list | None = None) -> dict:
        event = {
            "pubkey": self._get_pubkey(),
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags or [],
            "content": content,
        }
        event["id"] = _compute_event_id(event)
        event["sig"] = self._sign_event(event)
        return event

    def _connect(self):
        import websockets
        return websockets.connect(self._relay_url)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="nostr",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        tags = [["picard", chunk.manifest_id]]
        event = self._build_event(encoded_chunk, kind=1, tags=tags)

        async with self._connect() as ws:
            await ws.send(json.dumps(["EVENT", event]))
            raw = await ws.recv()
            resp = json.loads(raw)
            if resp[0] == "OK" and resp[2] is True:
                return StoredChunkRef(adapter_name="nostr", location=resp[1])
            raise RuntimeError(f"relay rejected event: {resp}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        async with self._connect() as ws:
            req = ["REQ", "picard-read", {"ids": [ref.location]}]
            await ws.send(json.dumps(req))
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg[0] == "EVENT" and msg[1] == "picard-read":
                    await ws.send(json.dumps(["CLOSE", "picard-read"]))
                    return msg[2]["content"]
                if msg[0] == "EOSE":
                    await ws.send(json.dumps(["CLOSE", "picard-read"]))
                    raise ValueError(f"event not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            tags = [["e", ref.location]]
            event = self._build_event("", kind=5, tags=tags)
            async with self._connect() as ws:
                await ws.send(json.dumps(["EVENT", event]))
                raw = await ws.recv()
                resp = json.loads(raw)
                return resp[0] == "OK" and resp[2] is True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        pubkey = self._get_pubkey()
        filt = {
            "kinds": [1],
            "authors": [pubkey],
            "#picard": [manifest_id],
        }
        refs: list[StoredChunkRef] = []
        async with self._connect() as ws:
            await ws.send(json.dumps(["REQ", "picard-list", filt]))
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg[0] == "EVENT" and msg[1] == "picard-list":
                    refs.append(StoredChunkRef(
                        adapter_name="nostr",
                        location=msg[2]["id"],
                    ))
                elif msg[0] == "EOSE":
                    break
            await ws.send(json.dumps(["CLOSE", "picard-list"]))
        return refs

    async def health_check(self) -> bool:
        try:
            async with self._connect() as ws:
                await ws.send(json.dumps(["REQ", "ping", {"limit": 0}]))
                raw = await ws.recv()
                msg = json.loads(raw)
                await ws.send(json.dumps(["CLOSE", "ping"]))
                return msg[0] == "EOSE"
        except Exception:
            return False
