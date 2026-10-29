"""DNS transaction ID adapter -- stores chunks in DNS query transaction IDs.

Each write_chunk encodes the chunk across a sequence of in-process DNS
query packets. Each UDP DNS query packet holds 2 bytes of chunk data in
its 16-bit transaction ID field. No network required. Stdlib struct only.

DNS query structure (RFC 1035): 2-byte TXID + 2-byte flags + 4x 2-byte counts
+ question section (QNAME + QTYPE + QCLASS).
"""
from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "dns_txid"

# Minimal DNS query suffix (flags=standard query, 1 question)
# Flags: QR=0, Opcode=0, AA=0, TC=0, RD=1, RA=0, Z=0, RCODE=0 = 0x0100
# QDCOUNT=1, ANCOUNT=0, NSCOUNT=0, ARCOUNT=0
_DNS_SUFFIX = struct.pack(">HHHH", 0x0100, 1, 0, 0)
# Question: picard.local A IN
_DNS_QUESTION = (
    b"\x06picard\x05local\x00"  # QNAME: picard.local
    + struct.pack(">HH", 1, 1)   # QTYPE=A, QCLASS=IN
)


def _make_dns_pkt(txid: int) -> bytes:
    return struct.pack(">H", txid) + _DNS_SUFFIX + _DNS_QUESTION


def _pad(data: bytes, block: int) -> bytes:
    rem = len(data) % block
    return data + b"\x00" * (block - rem if rem else 0)


class DnsTxidAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[list[bytes], str]] = {}  # token -> (pkts, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=True,
            tags={"requires": "stdlib", "substrate": "dns-transaction-id"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            raw = encoded_chunk.encode("utf-8")
            padded = _pad(raw, 2)
            pkts = []
            for i in range(0, len(padded), 2):
                txid = struct.unpack(">H", padded[i:i+2])[0]
                pkts.append(_make_dns_pkt(txid))
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
            raise KeyError(f"dns_txid chunk not in registry: {token}")
        pkts, _ = self._reg[token]
        raw = b"".join(struct.pack(">H", struct.unpack_from(">H", pkt, 0)[0]) for pkt in pkts)
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
            pkt = _make_dns_pkt(0xABCD)
            txid = struct.unpack_from(">H", pkt, 0)[0]
            return txid == 0xABCD
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
