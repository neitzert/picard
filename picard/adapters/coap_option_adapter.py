"""CoAP option adapter -- stores chunks in CoAP message option fields.

Each write_chunk encodes the chunk as a CoAP option value in a synthetic
CoAP packet using option number 2048 (experimental range). The packet bytes
are the substrate; data is also held in-process registry. No network needed.

CoAP packet structure (RFC 7252): 4-byte fixed header + token + options + payload.
Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "coap_option"

# CoAP option number 2048 (0x800) is in the experimental range
_OPT_PICARD = 2048


def _encode_option_delta_len(delta: int, length: int) -> bytes:
    """Encode a CoAP option header (delta + length + extended bytes)."""
    def _encode_field(val: int) -> tuple[int, bytes]:
        if val < 13:
            return val, b""
        elif val < 269:
            return 13, struct.pack("B", val - 13)
        else:
            return 14, struct.pack(">H", val - 269)

    d_nibble, d_ext = _encode_field(delta)
    l_nibble, l_ext = _encode_field(length)
    return bytes([(d_nibble << 4) | l_nibble]) + d_ext + l_ext


def _make_coap_packet(token_hex: str, data: bytes) -> bytes:
    """Build a minimal CoAP GET packet with a single Picard option."""
    tok = bytes.fromhex(token_hex[:8])
    # Fixed header: Ver=1, T=0 (CON), TKL=4, Code=0.01 (GET), MsgID=0x1234
    header = struct.pack(">BBH", (1 << 6) | (0 << 4) | len(tok), 0x01, 0x1234)
    # Option: delta from 0 to _OPT_PICARD, length = len(data)
    opt_header = _encode_option_delta_len(_OPT_PICARD, len(data))
    return header + tok + opt_header + data + b"\xff"  # FF = payload marker (empty payload)


def _extract_coap_option(pkt: bytes) -> bytes:
    """Extract the Picard option value from a CoAP packet."""
    # Skip 4-byte fixed header and TKL bytes of token
    tkl = pkt[0] & 0x0F
    pos = 4 + tkl
    # Parse first option
    d_nibble = (pkt[pos] >> 4) & 0x0F
    l_nibble = pkt[pos] & 0x0F
    pos += 1
    if d_nibble == 13:
        pos += 1
    elif d_nibble == 14:
        pos += 2
    opt_len = l_nibble
    if l_nibble == 13:
        opt_len = pkt[pos] + 13
        pos += 1
    elif l_nibble == 14:
        opt_len = struct.unpack_from(">H", pkt, pos)[0] + 269
        pos += 2
    return pkt[pos:pos + opt_len]


class CoapOptionAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[bytes, str]] = {}  # token -> (pkt, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "coap-option-field"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            data = encoded_chunk.encode("utf-8")
            pkt = _make_coap_packet(token, data)
            return token, pkt

        token, pkt = await asyncio.to_thread(_write)
        self._reg[token] = (pkt, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"coap_option chunk not in registry: {token}")
        pkt, _ = self._reg[token]
        data = _extract_coap_option(pkt)
        return data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            pkt = _make_coap_packet("deadbeef", b"test")
            return _extract_coap_option(pkt) == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
