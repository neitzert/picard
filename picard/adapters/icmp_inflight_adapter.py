"""ICMP in-flight adapter -- stores chunks as circulating ICMP echo payloads.

Data stored by this adapter has no at-rest state. Chunk bytes live exclusively
as ICMP echo request payloads circulating on the loopback interface. A
background relay thread intercepts each echo reply via an AF_PACKET raw socket
(scapy AsyncSniffer) before the kernel delivers it to any ICMP listener, then
re-injects a new echo request with the same payload. The packet never touches
disk, a file descriptor, or a named kernel object.

Mechanism:
  write_chunk(): split the encoded chunk into 1392-byte fragments, assign each
    fragment a unique ICMP ID, inject ICMP echo requests on the loopback
    interface, and register each ID in the relay thread's store dict.
  read_chunk(): look up each fragment ID in the relay dict, reassemble in
    order, return the encoded chunk string. Non-destructive -- the relay
    continues uninterrupted.
  delete_chunk(): remove fragment IDs from the relay dict. Packets die
    naturally (no reply re-injected, kernel TTL expires on loopback after the
    next unanswered echo).

Prior art: Loki (Phrack #49, 1996, daemon9/route) demonstrated ICMP echo as a
bidirectional covert channel by intercepting replies at the raw socket layer.
Ptunnel (Stodle, 2004) proved the model sustains ~100 KB/s through NAT. This
adapter extends the concept to self-contained storage: single-host, no remote
reflector needed.

Platform: Linux x86-64. Requires CAP_NET_RAW (or root) for AF_PACKET sockets.
macOS and Windows are not supported (different raw socket APIs and loopback
packet capture semantics).

Persistence: NONE. Data is lost when the process exits or the relay thread
crashes. Use only as an ephemeral, process-lifetime substrate.

EXPERIMENTAL: Run at your own risk. This adapter uses raw packet injection on
the loopback interface. It has not been tested across all kernel versions or
container configurations. Requires --cap-add NET_RAW in Docker.

WARNING: Use only on hardware and networks you own. Raw socket access on
shared infrastructure may be prohibited.

Requires: pip install scapy>=2.5
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import struct
import sys
import threading
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_NAME = "icmp-inflight"
_MAGIC = b"PICD"           # 4-byte magic prefix in every fragment payload
_HEADER_SIZE = 8            # magic(4) + frag_count(2) + frag_idx(2)
_MAX_PAYLOAD  = 1400        # MTU-safe ICMP payload size (Ethernet 1500 - IP/ICMP headers)
_FRAG_DATA    = _MAX_PAYLOAD - _HEADER_SIZE   # 1392 bytes of chunk data per fragment

# ---------------------------------------------------------------------------
# Optional scapy import
# ---------------------------------------------------------------------------

try:
    from scapy.all import IP, ICMP, sendp, AsyncSniffer   # type: ignore[import]
    _SCAPY_OK = True
except ImportError:
    _SCAPY_OK = False


def _require_scapy() -> None:
    if not _SCAPY_OK:
        raise ImportError(
            "scapy is required for icmp-inflight: pip install scapy>=2.5"
        )


def _require_linux() -> None:
    if sys.platform != "linux":
        raise NotImplementedError(
            "icmp-inflight requires Linux (AF_PACKET loopback capture)"
        )


# ---------------------------------------------------------------------------
# Relay thread
# ---------------------------------------------------------------------------

class _Relay:
    """Background thread that keeps ICMP fragments circulating on loopback.

    The store dict maps ICMP ID -> raw fragment payload bytes. On each captured
    echo reply whose ID is in the store, we re-inject a new echo request with
    the same payload and an incremented sequence number.

    AF_PACKET captures packets at the link layer, below icmp_rcv(), so replies
    are intercepted before the kernel's ICMP handler processes them.
    """

    def __init__(self, interface: str = "lo") -> None:
        self._interface = interface
        self._store: dict[int, bytes] = {}       # icmp_id -> payload bytes
        self._seq: dict[int, int] = {}           # icmp_id -> last seq number
        self._lock = threading.Lock()
        self._sniffer: object | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        _require_scapy()
        self._sniffer = AsyncSniffer(
            iface=self._interface,
            filter="icmp and icmp[icmptype] == 0",  # type 0 = echo reply
            prn=self._on_reply,
            store=False,
        )
        self._sniffer.start()   # type: ignore[union-attr]
        self._started = True

    def stop(self) -> None:
        if self._sniffer is not None and self._started:
            try:
                self._sniffer.stop()    # type: ignore[union-attr]
            except Exception:
                pass
        self._started = False

    def register(self, icmp_id: int, payload: bytes) -> None:
        with self._lock:
            self._store[icmp_id] = payload
            self._seq[icmp_id] = 0

    def unregister(self, icmp_id: int) -> None:
        with self._lock:
            self._store.pop(icmp_id, None)
            self._seq.pop(icmp_id, None)

    def get(self, icmp_id: int) -> bytes | None:
        with self._lock:
            return self._store.get(icmp_id)

    def _on_reply(self, pkt: object) -> None:
        try:
            icmp_id = pkt[ICMP].id      # type: ignore[index]
            with self._lock:
                if icmp_id not in self._store:
                    return
                payload = self._store[icmp_id]
                self._seq[icmp_id] = (self._seq.get(icmp_id, 0) + 1) & 0xFFFF
                seq = self._seq[icmp_id]

            new_pkt = (
                IP(dst="127.0.0.1")                         # type: ignore[call-arg]
                / ICMP(type=8, id=icmp_id, seq=seq)         # type: ignore[call-arg]
                / payload
            )
            sendp(new_pkt, iface=self._interface, verbose=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fragment encoding helpers
# ---------------------------------------------------------------------------

def _encode_fragment(frag_count: int, frag_idx: int, data: bytes) -> bytes:
    """Pack a fragment into the ICMP payload wire format."""
    return _MAGIC + struct.pack(">HH", frag_count, frag_idx) + data


def _decode_fragment(payload: bytes) -> tuple[int, int, bytes]:
    """Unpack (frag_count, frag_idx, data) from a fragment payload."""
    if len(payload) < _HEADER_SIZE or payload[:4] != _MAGIC:
        raise ValueError("invalid PICARD fragment magic")
    frag_count, frag_idx = struct.unpack(">HH", payload[4:8])
    return frag_count, frag_idx, payload[8:]


def _fragment(data: bytes) -> list[bytes]:
    """Split data into fragment payloads ready for injection."""
    chunks = [data[i:i + _FRAG_DATA] for i in range(0, max(len(data), 1), _FRAG_DATA)]
    return [_encode_fragment(len(chunks), idx, c) for idx, c in enumerate(chunks)]


def _reassemble(fragments: dict[int, bytes]) -> bytes:
    """Reassemble fragments by index order."""
    return b"".join(fragments[i] for i in sorted(fragments))


# ---------------------------------------------------------------------------
# ID allocation
# ---------------------------------------------------------------------------

class _IdAllocator:
    """Thread-safe pool of 16-bit ICMP IDs."""

    def __init__(self, base: int | None = None) -> None:
        self._next = (base if base is not None else random.randint(1000, 60000)) & 0xFFFF
        self._lock = threading.Lock()
        self._in_use: set[int] = set()

    def alloc(self) -> int:
        with self._lock:
            for _ in range(65535):
                cand = self._next
                self._next = (self._next + 1) & 0xFFFF or 1
                if cand not in self._in_use:
                    self._in_use.add(cand)
                    return cand
        raise RuntimeError("ICMP ID space exhausted")

    def free(self, icmp_id: int) -> None:
        with self._lock:
            self._in_use.discard(icmp_id)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class IcmpInflightAdapter(Adapter):
    """Store chunks as ICMP echo payloads circulating on the loopback interface.

    Data has no at-rest state. It exists only as packets scheduled for
    re-transmission by a relay thread. Process exit equals data loss.

    Usage::

        adapter = IcmpInflightAdapter()
        ref = await adapter.write_chunk(encoded_chunk)
        result = await adapter.read_chunk(ref)
        await adapter.delete_chunk(ref)
        await adapter.close()

    Geth nodes are not involved. No network egress occurs in loopback mode.
    """

    def __init__(
        self,
        interface: str = "lo",
        max_concurrent_fragments: int = 256,
        icmp_id_base: int | None = None,
    ) -> None:
        _require_linux()
        _require_scapy()

        self._interface = interface
        self._max_fragments = max_concurrent_fragments
        self._relay = _Relay(interface)
        self._allocator = _IdAllocator(icmp_id_base)
        # manifest_id -> list of icmp_ids
        self._registry: dict[str, list[int]] = {}
        self._relay.start()

    # ------------------------------------------------------------------
    # Adapter protocol
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_fragments * _FRAG_DATA,
            persistent=False,
            ordered=False,
            rate_limit_per_min=120,
            tags={
                "tier": "4",
                "experimental": "true",
                "requires": "CAP_NET_RAW,Linux",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        frags = _fragment(data)
        if len(frags) > self._max_fragments:
            raise ValueError(
                f"chunk requires {len(frags)} fragments but max_concurrent_fragments="
                f"{self._max_fragments}"
            )

        ids: list[int] = []
        for frag_payload in frags:
            icmp_id = self._allocator.alloc()
            ids.append(icmp_id)
            self._relay.register(icmp_id, frag_payload)

            # Initial injection -- relay takes over after the first reply
            await asyncio.get_event_loop().run_in_executor(
                None, self._inject, icmp_id, frag_payload
            )

        location = json.dumps({
            "icmp_ids": ids,
            "frag_count": len(frags),
            "data_len": len(data),
        })
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        ids: list[int] = loc["icmp_ids"]
        frag_count: int = loc["frag_count"]
        data_len: int = loc["data_len"]

        fragments: dict[int, bytes] = {}
        for i, icmp_id in enumerate(ids):
            payload = self._relay.get(icmp_id)
            if payload is None:
                raise KeyError(f"fragment {i} (ICMP ID {icmp_id}) not found in relay")
            _, frag_idx, chunk_bytes = _decode_fragment(payload)
            fragments[frag_idx] = chunk_bytes

        if len(fragments) != frag_count:
            raise ValueError(
                f"expected {frag_count} fragments, got {len(fragments)}"
            )

        data = _reassemble(fragments)[:data_len]
        return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        ids: list[int] = loc["icmp_ids"]
        found = False
        for icmp_id in ids:
            if self._relay.get(icmp_id) is not None:
                found = True
            self._relay.unregister(icmp_id)
            self._allocator.free(icmp_id)
        return found

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        ids = self._registry.get(manifest_id, [])
        if not ids:
            return []
        location = json.dumps({"icmp_ids": ids, "frag_count": len(ids), "data_len": 0})
        return [StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)]

    async def health_check(self) -> bool:
        if not _SCAPY_OK:
            return False
        if sys.platform != "linux":
            return False
        # Probe CAP_NET_RAW by attempting to open an AF_PACKET socket
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
            s.close()
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    async def close(self) -> None:
        self._relay.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _inject(self, icmp_id: int, payload: bytes) -> None:
        pkt = (
            IP(dst="127.0.0.1")                         # type: ignore[call-arg]
            / ICMP(type=8, id=icmp_id, seq=0)           # type: ignore[call-arg]
            / payload
        )
        sendp(pkt, iface=self._interface, verbose=False)
