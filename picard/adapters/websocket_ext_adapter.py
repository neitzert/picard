"""WebSocket extension adapter -- stores chunks in WebSocket frame masking.

Each write_chunk builds a synthetic WebSocket binary frame (RFC 6455,
opcode 0x02) with a deterministic 4-byte masking key derived from the
chunk's token. The masked payload and frame bytes are held in-process.
Cold reads unmask and return the payload. Stdlib only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "websocket_ext"

# WebSocket opcodes
_OP_BINARY = 0x02


def _mask_payload(data: bytes, mask: bytes) -> bytes:
    return bytes(data[i] ^ mask[i % 4] for i in range(len(data)))


def _make_ws_frame(data: bytes, mask: bytes) -> bytes:
    """Build a masked WebSocket binary frame."""
    masked = _mask_payload(data, mask)
    length = len(data)
    # FIN=1, RSV1=0, RSV2=0, RSV3=0, opcode=0x02 -- first byte = 0x82
    first = 0x82
    if length < 126:
        header = struct.pack("BB", first, 0x80 | length)
    elif length < 65536:
        header = struct.pack(">BBH", first, 0xFE, length)
    else:
        header = struct.pack(">BBQ", first, 0xFF, length)
    return header + mask + masked


def _parse_ws_frame(frame: bytes) -> bytes:
    """Extract unmasked payload from a WebSocket frame."""
    second = frame[1]
    is_masked = bool(second & 0x80)
    length_nibble = second & 0x7F
    pos = 2
    if length_nibble == 126:
        length = struct.unpack_from(">H", frame, pos)[0]; pos += 2
    elif length_nibble == 127:
        length = struct.unpack_from(">Q", frame, pos)[0]; pos += 8
    else:
        length = length_nibble
    if is_masked:
        mask = frame[pos:pos+4]; pos += 4
        return _mask_payload(frame[pos:pos+length], mask)
    return frame[pos:pos+length]


class WebsocketExtAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[bytes, str]] = {}  # token -> (frame, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "websocket-frame-masked-payload"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            data = encoded_chunk.encode("utf-8")
            # Deterministic mask from token hash
            mask = hashlib.sha256(token.encode()).digest()[:4]
            frame = _make_ws_frame(data, mask)
            return token, frame

        token, frame = await asyncio.to_thread(_write)
        self._reg[token] = (frame, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"websocket_ext chunk not in registry: {token}")
        frame, _ = self._reg[token]
        return _parse_ws_frame(frame).decode("utf-8")

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
            data = b"hello world"
            mask = b"\x37\xfa\x21\x3d"
            frame = _make_ws_frame(data, mask)
            return _parse_ws_frame(frame) == data
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
