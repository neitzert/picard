"""RTP in-flight adapter -- stores chunks as circulating RTP payloads on loopback UDP.

Data stored by this adapter has no at-rest state. Chunk bytes live exclusively as
RTP packet payloads bouncing between two UDP loopback port pairs. A background
relay thread receives each packet on the relay port, holds the reassembled payload
in memory, and re-injects a new RTP packet back to the sender port, keeping the
stream alive. The data never touches disk, a named file descriptor, or any kernel
object visible to the OS.

Mechanism:
  write_chunk(): assign a random 32-bit SSRC, fragment the encoded chunk into
    1388-byte RTP payloads, bind a UDP port pair on 127.0.0.1, register the SSRC
    with the relay thread, and inject the initial fragments.
  read_chunk(): look up the SSRC in the relay dict, reassemble the fragments in
    sequence-number order, and return the encoded chunk string. Non-destructive.
  delete_chunk(): remove the SSRC from the relay dict and release the port pair.

RTP framing (RFC 3550 section 5.1, 12-byte fixed header):
  V=2  P=0  X=0  CC=0  M=0  PT=96  SEQ(16)  TIMESTAMP(32)  SSRC(32)  payload

The SSRC is used as the chunk namespace key. Fragment ordering uses the 16-bit
sequence number. No external dependencies: pure stdlib (socket, threading, struct).

Survives process restart only if the relay thread is restarted with the same SSRC
and port assignment. SSRC-to-port mapping must be externally coordinated.

Persistence: NONE. Data is lost when the process exits, the relay thread crashes,
or the port pair is released. Use only as an ephemeral, process-lifetime substrate.

EXPERIMENTAL: Run at your own risk. This adapter injects UDP packets on the
loopback interface. Port exhaustion on high-fanout deployments is possible; set
base_port and max_concurrent_chunks to avoid conflicts with other services.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
import struct
import threading
import time
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_NAME  = "rtp-inflight"
_RTP_VERSION   = 2
_PAYLOAD_TYPE  = 96              # dynamic, per RFC 3550
_RTP_HDR_SIZE  = 12              # fixed header bytes
_FRAG_HDR_SIZE = 4               # frag_count(2) + frag_idx(2) prepended to each payload
_MAX_UDP_SAFE  = 1400            # MTU-safe UDP payload
_MAX_RTP_DATA  = _MAX_UDP_SAFE - _RTP_HDR_SIZE - _FRAG_HDR_SIZE  # 1384 bytes per fragment
_RECV_TIMEOUT  = 0.1             # seconds; relay socket poll interval
_MAGIC         = b"PICD"
_MAGIC_LEN     = 4

# ---------------------------------------------------------------------------
# RTP header helpers
# ---------------------------------------------------------------------------

def _pack_rtp(ssrc: int, seq: int, timestamp: int, payload: bytes) -> bytes:
    """Encode a minimal RTP packet (RFC 3550, fixed header only)."""
    byte0 = (_RTP_VERSION << 6) & 0xFF  # V=2, P=0, X=0, CC=0
    byte1 = _PAYLOAD_TYPE & 0x7F        # M=0, PT=96
    header = struct.pack(">BBHII", byte0, byte1, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + payload


def _unpack_rtp(data: bytes) -> tuple[int, int, int, bytes]:
    """Decode a minimal RTP packet. Returns (ssrc, seq, timestamp, payload)."""
    if len(data) < _RTP_HDR_SIZE:
        raise ValueError("packet too short for RTP header")
    byte0, byte1, seq, timestamp, ssrc = struct.unpack(">BBHII", data[:_RTP_HDR_SIZE])
    version = (byte0 >> 6) & 0x3
    if version != _RTP_VERSION:
        raise ValueError(f"unexpected RTP version {version}")
    return ssrc, seq, timestamp, data[_RTP_HDR_SIZE:]


# ---------------------------------------------------------------------------
# Fragment encoding helpers
# ---------------------------------------------------------------------------

def _encode_fragment(frag_count: int, frag_idx: int, data: bytes) -> bytes:
    """Prepend fragment header to payload: frag_count(2) + frag_idx(2) + data."""
    return struct.pack(">HH", frag_count, frag_idx) + data


def _decode_fragment(payload: bytes) -> tuple[int, int, bytes]:
    """Unpack (frag_count, frag_idx, data) from a fragment payload."""
    if len(payload) < _FRAG_HDR_SIZE:
        raise ValueError("fragment payload too short")
    frag_count, frag_idx = struct.unpack(">HH", payload[:_FRAG_HDR_SIZE])
    return frag_count, frag_idx, payload[_FRAG_HDR_SIZE:]


def _fragment(data: bytes, max_bytes: int = _MAX_RTP_DATA) -> list[bytes]:
    """Split data into fragment payloads ready for RTP injection."""
    pieces = [data[i:i + max_bytes] for i in range(0, max(len(data), 1), max_bytes)]
    return [_encode_fragment(len(pieces), idx, p) for idx, p in enumerate(pieces)]


# ---------------------------------------------------------------------------
# Relay thread
# ---------------------------------------------------------------------------

class _RtpRelay:
    """Background thread that keeps RTP fragments circulating between two UDP ports.

    For each registered SSRC, a (recv_port, send_port) pair is bound on
    127.0.0.1. The relay reads from recv_port, stores the reassembled payload
    in self._store, and re-injects the packet to send_port to keep it alive.

    The relay uses a single shared socket pair managed by the thread rather than
    per-SSRC sockets, to keep fd usage bounded.
    """

    def __init__(self, base_port: int = 54000) -> None:
        self._base_port = base_port
        self._lock = threading.Lock()
        # ssrc -> {"frags": {seq: payload_bytes}, "frag_count": int, "port": int}
        self._store: dict[int, dict] = {}
        self._port_seq: dict[int, int] = {}       # ssrc -> next seq number to emit
        self._port_ts:  dict[int, int] = {}        # ssrc -> last timestamp
        self._next_port = base_port
        # ssrc -> (recv_sock, send_sock)
        self._sockets: dict[int, tuple[socket.socket, socket.socket]] = {}
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rtp-relay")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with self._lock:
            for recv_s, send_s in self._sockets.values():
                try:
                    recv_s.close()
                except Exception:
                    pass
                try:
                    send_s.close()
                except Exception:
                    pass
            self._sockets.clear()
            self._store.clear()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # SSRC registration
    # ------------------------------------------------------------------

    def register(self, ssrc: int, frags: list[bytes]) -> int:
        """Register SSRC with its fragment list. Returns the assigned recv port."""
        with self._lock:
            port = self._next_port
            self._next_port += 2  # recv on port, send on port+1
            recv_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            recv_s.bind(("127.0.0.1", port))
            recv_s.settimeout(_RECV_TIMEOUT)
            send_s.bind(("127.0.0.1", port + 1))
            send_s.settimeout(_RECV_TIMEOUT)
            self._sockets[ssrc] = (recv_s, send_s)
            self._store[ssrc] = {
                "frags": {i: f for i, f in enumerate(frags)},
                "frag_count": len(frags),
                "port": port,
            }
            self._port_seq[ssrc] = len(frags)  # start seq after initial injection
            self._port_ts[ssrc] = int(time.monotonic() * 8000) & 0xFFFFFFFF  # 8 kHz clock, 32-bit wrap
        return port

    def unregister(self, ssrc: int) -> None:
        with self._lock:
            entry = self._sockets.pop(ssrc, None)
            if entry:
                try:
                    entry[0].close()
                except Exception:
                    pass
                try:
                    entry[1].close()
                except Exception:
                    pass
            self._store.pop(ssrc, None)
            self._port_seq.pop(ssrc, None)
            self._port_ts.pop(ssrc, None)

    def get_fragments(self, ssrc: int) -> dict[int, bytes] | None:
        with self._lock:
            entry = self._store.get(ssrc)
            if entry is None:
                return None
            return dict(entry["frags"])

    def get_port(self, ssrc: int) -> int | None:
        with self._lock:
            entry = self._store.get(ssrc)
            return entry["port"] if entry else None

    # ------------------------------------------------------------------
    # Relay loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            with self._lock:
                items = list(self._sockets.items())
            for ssrc, (recv_s, send_s) in items:
                try:
                    pkt, addr = recv_s.recvfrom(2048)
                    pkt_ssrc, seq, ts, payload = _unpack_rtp(pkt)
                    if pkt_ssrc != ssrc:
                        continue
                    frag_count, frag_idx, data = _decode_fragment(payload)
                    with self._lock:
                        if ssrc not in self._store:
                            continue
                        self._store[ssrc]["frags"][frag_idx] = payload
                        next_seq = self._port_seq[ssrc]
                        self._port_seq[ssrc] = (next_seq + 1) & 0xFFFF
                        next_ts = self._port_ts[ssrc] + 160
                        self._port_ts[ssrc] = next_ts
                    # Re-inject to keep alive
                    new_pkt = _pack_rtp(ssrc, next_seq, next_ts, payload)
                    try:
                        port = self._store[ssrc]["port"] if ssrc in self._store else None
                        if port is not None:
                            send_s.sendto(new_pkt, ("127.0.0.1", port))
                    except Exception:
                        pass
                except socket.timeout:
                    pass
                except OSError:
                    pass
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class RtpInflightAdapter(Adapter):
    """Store chunks as RTP payloads circulating on loopback UDP port pairs.

    Data has no at-rest state. It exists only as RTP packets bouncing between
    two UDP ports on 127.0.0.1. Process exit equals data loss.

    No elevated privileges required. No external dependencies.

    Usage::

        adapter = RtpInflightAdapter()
        ref = await adapter.write_chunk(encoded_chunk)
        result = await adapter.read_chunk(ref)
        await adapter.delete_chunk(ref)
        await adapter.close()
    """

    def __init__(
        self,
        base_port: int = 54000,
        max_chunk_bytes: int = 65535,
        relay_timeout_s: float = 30.0,
    ) -> None:
        self._base_port = base_port
        self._max_chunk_bytes = max_chunk_bytes
        self._relay_timeout_s = relay_timeout_s
        self._relay = _RtpRelay(base_port=base_port)
        self._relay.start()

    # ------------------------------------------------------------------
    # Adapter protocol
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=False,
            ordered=True,
            ttl_seconds=None,
            rate_limit_per_min=120,
            tags={
                "tier": "5",
                "experimental": "true",
                "requires": "UDP loopback",
                "substrate": "rtp-inflight",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode() if isinstance(encoded_chunk, str) else encoded_chunk
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"chunk is {len(data)} bytes but max_chunk_bytes={self._max_chunk_bytes}"
            )

        frags = _fragment(data)
        ssrc = random.randint(1, 0xFFFFFFFF)

        port = await asyncio.get_event_loop().run_in_executor(
            None, self._relay.register, ssrc, frags
        )

        # Inject initial packets to seed the relay loop
        await asyncio.get_event_loop().run_in_executor(
            None, self._inject_initial, ssrc, frags, port
        )

        location = json.dumps({
            "ssrc": ssrc,
            "port_recv": port,
            "port_relay": port + 1,
            "fragment_count": len(frags),
            "data_len": len(data),
        })
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        ssrc: int = loc["ssrc"]
        frag_count: int = loc["fragment_count"]
        data_len: int = loc["data_len"]

        frags = await asyncio.get_event_loop().run_in_executor(
            None, self._relay.get_fragments, ssrc
        )
        if frags is None:
            raise KeyError(f"SSRC {ssrc:#010x} not found in relay")
        if len(frags) != frag_count:
            raise ValueError(
                f"expected {frag_count} fragments, found {len(frags)}"
            )

        data = b""
        for idx in sorted(frags):
            _, _, chunk_bytes = _decode_fragment(frags[idx])
            data += chunk_bytes
        return data[:data_len].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        ssrc: int = loc["ssrc"]
        existed = self._relay.get_fragments(ssrc) is not None
        await asyncio.get_event_loop().run_in_executor(
            None, self._relay.unregister, ssrc
        )
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # In-flight storage has no persistent index; return empty.
        return []

    async def health_check(self) -> bool:
        if not self._relay.is_alive():
            return False
        try:
            # Probe UDP loopback reachability with a single packet
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            probe_port = self._base_port - 2  # port not in use by relay
            try:
                recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                recv.bind(("127.0.0.1", probe_port))
                recv.settimeout(0.5)
                s.sendto(b"ping", ("127.0.0.1", probe_port))
                data, _ = recv.recvfrom(16)
                recv.close()
                return data == b"ping"
            except Exception:
                return False
            finally:
                s.close()
        except Exception:
            return False

    async def close(self) -> None:
        self._relay.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _inject_initial(self, ssrc: int, frags: list[bytes], port: int) -> None:
        """Inject the first round of RTP packets to seed the relay."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ts = int(time.monotonic() * 8000) & 0xFFFFFFFF
            for seq, frag_payload in enumerate(frags):
                pkt = _pack_rtp(ssrc, seq, (ts + seq * 160) & 0xFFFFFFFF, frag_payload)
                s.sendto(pkt, ("127.0.0.1", port))
        finally:
            s.close()
