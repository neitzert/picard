"""NTP timestamp adapter -- stores chunks in NTP packet transmit timestamps.

Each write_chunk encodes the chunk across a sequence of in-process NTP
packets, each carrying 8 bytes of chunk data in the 64-bit transmit
timestamp field. No network required. Stdlib struct only.
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ntp_ts"

# NTP packet: 48 bytes minimum
# LI=0, VN=4, Mode=3 (client): first byte = 0x23
_NTP_HDR = struct.pack(">B", 0x23) + b"\x00" * 39  # 40 bytes: header through ref ts
# Transmit timestamp is at bytes 40-47


def _make_ntp_pkt(data_8: bytes) -> bytes:
    """Build an NTP client packet with data_8 as the transmit timestamp."""
    assert len(data_8) == 8
    return _NTP_HDR + data_8


def _pad(data: bytes, block: int) -> bytes:
    rem = len(data) % block
    return data + b"\x00" * (block - rem if rem else 0)


class NtpTsAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[list[bytes], str]] = {}  # token -> (pkts, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=True,
            tags={"requires": "stdlib", "substrate": "ntp-transmit-timestamp"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            raw = encoded_chunk.encode("utf-8")
            padded = _pad(raw, 8)
            pkts = [_make_ntp_pkt(padded[i:i + 8]) for i in range(0, len(padded), 8)]
            return token, pkts

        token, pkts = await asyncio.to_thread(_write)
        self._reg[token] = (pkts, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                               "n_pkts": len(pkts),
                               "orig_len": len(encoded_chunk.encode("utf-8"))}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token not in self._reg:
            raise KeyError(f"ntp_ts chunk not in registry: {token}")
        pkts, _ = self._reg[token]
        raw = b"".join(pkt[40:48] for pkt in pkts)
        orig_len = loc["orig_len"]
        return raw[:orig_len].decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        self._reg.pop(token, None)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (pkts, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "n_pkts": len(pkts),
                                                "orig_len": len(data.encode("utf-8"))})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            data = b"testtest"
            pkt = _make_ntp_pkt(data)
            return pkt[40:48] == data
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
