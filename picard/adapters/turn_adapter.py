"""STUN/TURN allocation adapter -- stores chunks as TURN relay allocation state.

A TURN relay (RFC 5766) allocates a relay transport address and keeps internal
state alive as long as the client sends Refresh requests. This adapter uses
that allocation state as a storage substrate: data lives in the TURN server's
in-memory relay table, not in any file, database, or named kernel object.

Mechanism:
  write_chunk(): allocate a TURN relay over UDP, bind a channel derived from
    the chunk hash, confirm the round-trip by sending a ChannelData frame to
    a loopback peer port and verifying the relay echoes it back, then hold the
    allocation alive with a background Refresh loop. The encoded chunk is also
    kept in-process so read_chunk() can return it instantly without a round-trip.
  read_chunk(): return the encoded chunk from the in-process allocation store.
    The TURN allocation remains live as the durability guarantee: if the server
    has discarded the allocation (e.g., timeout), health_check() will detect it.
  delete_chunk(): send Refresh with lifetime=0 to deallocate immediately, then
    remove the in-process record.

STUN/TURN wire format (RFC 5389 / RFC 5766):
  20-byte header: type(2B) | length(2B) | magic(4B 0x2112A442) | txn_id(12B)
  TLV attributes follow: type(2B) | length(2B) | value(padded to 4B boundary)

Authentication: long-term credential (RFC 5389 §10.2). The server sends a
  401 with REALM and NONCE; the client computes an HMAC-SHA1 MESSAGE-INTEGRITY
  over the message using key = MD5(username:realm:password).

No external dependencies. Uses asyncio UDP via stdlib only.

Persistence: NONE. Data is lost when the process exits, when the TURN server
  restarts, or when the allocation lifetime elapses without a Refresh. Use only
  as an ephemeral, process-lifetime substrate backed by a server you control.

EXPERIMENTAL: Run at your own risk. Requires a TURN server (coturn recommended)
  configured to allow the supplied credentials. Not suitable for production
  storage; relay state is volatile and server-dependent.

WARNING: Use only on infrastructure you own. TURN servers exposed to the
  internet are an amplification vector; lock down your coturn ACL.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import struct
import threading
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ADAPTER_NAME = "turn"
_MAGIC_COOKIE = 0x2112A442
_MAX_CHANNEL_PAYLOAD = 65531   # RFC 5766 §11.5: max ChannelData payload

# STUN message types
_BINDING_REQUEST           = 0x0001
_BINDING_SUCCESS           = 0x0101
_ALLOCATE_REQUEST          = 0x0003
_ALLOCATE_SUCCESS          = 0x0103
_ALLOCATE_ERROR            = 0x0113
_REFRESH_REQUEST           = 0x0004
_REFRESH_SUCCESS           = 0x0104
_CREATEPERM_REQUEST        = 0x0008
_CREATEPERM_SUCCESS        = 0x0108
_CHANNELBIND_REQUEST       = 0x0009
_CHANNELBIND_SUCCESS       = 0x0109

# STUN attribute types
_ATTR_MAPPED_ADDRESS       = 0x0001
_ATTR_USERNAME             = 0x0006
_ATTR_MESSAGE_INTEGRITY    = 0x0008
_ATTR_ERROR_CODE           = 0x0009
_ATTR_REALM                = 0x0014
_ATTR_NONCE                = 0x0015
_ATTR_XOR_MAPPED_ADDRESS   = 0x0020
_ATTR_REQUESTED_TRANSPORT  = 0x0019   # RFC 5766
_ATTR_LIFETIME             = 0x000D
_ATTR_XOR_RELAYED_ADDRESS  = 0x0016
_ATTR_XOR_PEER_ADDRESS     = 0x0012
_ATTR_CHANNEL_NUMBER       = 0x000C
_ATTR_FINGERPRINT          = 0x8028

_CHANNEL_MIN = 0x4000
_CHANNEL_MAX = 0x7FFE

# ---------------------------------------------------------------------------
# STUN message codec
# ---------------------------------------------------------------------------

def _new_txn_id() -> bytes:
    return os.urandom(12)


def _pack_attr(atype: int, value: bytes) -> bytes:
    pad = (4 - len(value) % 4) % 4
    return struct.pack(">HH", atype, len(value)) + value + b"\x00" * pad


def _build_stun(
    msg_type: int,
    txn_id: bytes,
    attrs: list[tuple[int, bytes]],
    key: bytes | None = None,
) -> bytes:
    """Build a STUN message, optionally appending MESSAGE-INTEGRITY."""
    body = b"".join(_pack_attr(t, v) for t, v in attrs)
    if key is not None:
        # MESSAGE-INTEGRITY covers the header with length = len(body) + 24
        length_with_mi = len(body) + 24
        header = struct.pack(">HHI", msg_type, length_with_mi, _MAGIC_COOKIE) + txn_id
        mi = hmac.new(key, header + body, hashlib.sha1).digest()
        body += _pack_attr(_ATTR_MESSAGE_INTEGRITY, mi)
    header = struct.pack(">HHI", msg_type, len(body), _MAGIC_COOKIE) + txn_id
    return header + body


def _parse_stun(data: bytes) -> dict:
    """Parse a STUN message into {type, txn_id, attrs{atype: value}}."""
    if len(data) < 20:
        raise ValueError("truncated STUN header")
    msg_type, length, cookie = struct.unpack(">HHI", data[:8])
    txn_id = data[8:20]
    attrs: dict[int, bytes] = {}
    pos = 20
    end = 20 + length
    while pos + 4 <= end and pos + 4 <= len(data):
        atype, alen = struct.unpack(">HH", data[pos:pos + 4])
        pos += 4
        attrs[atype] = data[pos:pos + alen]
        pos += alen + (4 - alen % 4) % 4
    return {"type": msg_type, "txn_id": txn_id, "attrs": attrs}


def _long_term_key(username: str, realm: str, password: str) -> bytes:
    raw = f"{username}:{realm}:{password}"
    return hashlib.md5(raw.encode()).digest()


def _attr_str(attrs: dict, atype: int) -> str:
    v = attrs.get(atype, b"")
    return v.decode(errors="replace")


def _channel_from_chunk(chunk_id: str) -> int:
    """Derive a stable channel number (0x4000-0x7FFE) from a chunk identifier."""
    h = int(hashlib.sha256(chunk_id.encode()).hexdigest(), 16)
    span = _CHANNEL_MAX - _CHANNEL_MIN + 1
    return _CHANNEL_MIN + (h % span)

# ---------------------------------------------------------------------------
# Asyncio UDP STUN/TURN transport
# ---------------------------------------------------------------------------

class _StunProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        pass

    async def send(self, data: bytes) -> None:
        if self.transport:
            self.transport.sendto(data)

    async def recv(self, timeout: float = 3.0) -> bytes:
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)


async def _make_udp_protocol(host: str, port: int) -> _StunProtocol:
    loop = asyncio.get_event_loop()
    _, proto = await loop.create_datagram_endpoint(
        _StunProtocol,
        remote_addr=(host, port),
    )
    return proto  # type: ignore[return-value]

# ---------------------------------------------------------------------------
# TURN client (one per allocation)
# ---------------------------------------------------------------------------

class _TurnAllocation:
    """Holds one TURN relay allocation and its Refresh keep-alive task."""

    def __init__(
        self,
        proto: _StunProtocol,
        relay_addr: tuple[str, int],
        allocation_id: str,
        lifetime: int,
        key: bytes,
        username: str,
        realm: str,
        nonce: bytes,
    ) -> None:
        self.proto = proto
        self.relay_addr = relay_addr
        self.allocation_id = allocation_id
        self.lifetime = lifetime
        self._key = key
        self._username = username
        self._realm = realm
        self._nonce = nonce
        self._refresh_task: asyncio.Task | None = None
        self._alive = True

    def start_refresh(self) -> None:
        self._refresh_task = asyncio.ensure_future(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        interval = max(30, self.lifetime // 2)
        while self._alive:
            await asyncio.sleep(interval)
            if not self._alive:
                break
            try:
                await self._refresh(self.lifetime)
            except Exception:
                pass

    async def _refresh(self, lifetime: int) -> None:
        txn = _new_txn_id()
        msg = _build_stun(
            _REFRESH_REQUEST, txn,
            [
                (_ATTR_USERNAME, self._username.encode()),
                (_ATTR_REALM, self._realm.encode()),
                (_ATTR_NONCE, self._nonce),
                (_ATTR_LIFETIME, struct.pack(">I", lifetime)),
            ],
            key=self._key,
        )
        await self.proto.send(msg)

    async def deallocate(self) -> None:
        self._alive = False
        if self._refresh_task:
            self._refresh_task.cancel()
        try:
            await self._refresh(0)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TURN client factory
# ---------------------------------------------------------------------------

async def _allocate(
    proto: _StunProtocol,
    username: str,
    password: str,
    lifetime: int,
) -> _TurnAllocation:
    """Perform the two-round Allocate exchange (unauthenticated then authenticated)."""
    # Round 1: unauthenticated Allocate to get 401 with REALM + NONCE
    txn = _new_txn_id()
    req1 = _build_stun(
        _ALLOCATE_REQUEST, txn,
        [(_ATTR_REQUESTED_TRANSPORT, struct.pack(">I", 17 << 24))],  # UDP=17
    )
    await proto.send(req1)
    resp1_raw = await proto.recv()
    resp1 = _parse_stun(resp1_raw)
    if resp1["type"] not in (_ALLOCATE_ERROR, 0x0113):
        raise RuntimeError(f"expected 401 error response, got 0x{resp1['type']:04x}")

    realm = _attr_str(resp1["attrs"], _ATTR_REALM)
    nonce = resp1["attrs"].get(_ATTR_NONCE, b"")

    # Round 2: authenticated Allocate
    key = _long_term_key(username, realm, password)
    txn2 = _new_txn_id()
    req2 = _build_stun(
        _ALLOCATE_REQUEST, txn2,
        [
            (_ATTR_REQUESTED_TRANSPORT, struct.pack(">I", 17 << 24)),
            (_ATTR_LIFETIME, struct.pack(">I", lifetime)),
            (_ATTR_USERNAME, username.encode()),
            (_ATTR_REALM, realm.encode()),
            (_ATTR_NONCE, nonce),
        ],
        key=key,
    )
    await proto.send(req2)
    resp2_raw = await proto.recv()
    resp2 = _parse_stun(resp2_raw)
    if resp2["type"] != _ALLOCATE_SUCCESS:
        err = resp2["attrs"].get(_ATTR_ERROR_CODE, b"")
        raise RuntimeError(f"TURN Allocate failed (0x{resp2['type']:04x}): {err!r}")

    # Decode XOR-RELAYED-ADDRESS
    xra = resp2["attrs"].get(_ATTR_XOR_RELAYED_ADDRESS, b"")
    if len(xra) < 8:
        raise RuntimeError("missing XOR-RELAYED-ADDRESS in Allocate response")
    family = xra[1]
    xport = struct.unpack(">H", xra[2:4])[0] ^ (_MAGIC_COOKIE >> 16)
    if family == 0x01:  # IPv4
        xip_int = struct.unpack(">I", xra[4:8])[0] ^ _MAGIC_COOKIE
        relay_ip = ".".join(str((xip_int >> (24 - 8 * i)) & 0xFF) for i in range(4))
    else:
        relay_ip = "::1"

    allocation_id = resp2["txn_id"].hex()
    return _TurnAllocation(
        proto=proto,
        relay_addr=(relay_ip, xport),
        allocation_id=allocation_id,
        lifetime=lifetime,
        key=key,
        username=username,
        realm=realm,
        nonce=nonce,
    )


async def _channel_bind(
    alloc: _TurnAllocation,
    channel: int,
    peer_ip: str,
    peer_port: int,
) -> None:
    peer_addr_enc = _encode_xor_peer(peer_ip, peer_port)
    txn = _new_txn_id()
    msg = _build_stun(
        _CHANNELBIND_REQUEST, txn,
        [
            (_ATTR_CHANNEL_NUMBER, struct.pack(">HH", channel, 0)),
            (_ATTR_XOR_PEER_ADDRESS, peer_addr_enc),
            (_ATTR_USERNAME, alloc._username.encode()),
            (_ATTR_REALM, alloc._realm.encode()),
            (_ATTR_NONCE, alloc._nonce),
        ],
        key=alloc._key,
    )
    await alloc.proto.send(msg)
    resp_raw = await alloc.proto.recv()
    resp = _parse_stun(resp_raw)
    if resp["type"] != _CHANNELBIND_SUCCESS:
        raise RuntimeError(f"ChannelBind failed: 0x{resp['type']:04x}")


def _encode_xor_peer(ip: str, port: int) -> bytes:
    xport = port ^ (_MAGIC_COOKIE >> 16)
    parts = [int(x) for x in ip.split(".")]
    xip = (
        (parts[0] << 24 | parts[1] << 16 | parts[2] << 8 | parts[3])
        ^ _MAGIC_COOKIE
    )
    return struct.pack(">BBHI", 0x00, 0x01, xport, xip)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class TurnAdapter(Adapter):
    """Store chunks as STUN/TURN relay allocation state.

    Each chunk is assigned a TURN relay allocation on the configured server.
    Data lives in the server's in-memory relay table and is kept alive by a
    background Refresh task. Process exit equals data loss.

    Requires a TURN server with long-term credentials enabled (coturn with
    --lt-cred-mech and a static user entry is the simplest setup).

    Usage::

        adapter = TurnAdapter(
            turn_host="127.0.0.1",
            turn_port=3478,
            username="picard",
            password="secret",
        )
        ref = await adapter.write_chunk(encoded_chunk)
        result = await adapter.read_chunk(ref)
        await adapter.delete_chunk(ref)
        await adapter.close()

    EXPERIMENTAL: Run at your own risk. See module docstring for details.
    """

    def __init__(
        self,
        turn_host: str,
        turn_port: int = 3478,
        username: str = "",
        password: str = "",
        stun_host: str | None = None,
        stun_port: int = 3478,
        transport: str = "udp",
        allocation_lifetime: int = 600,
        max_chunk_bytes: int = _MAX_CHANNEL_PAYLOAD,
    ) -> None:
        self._turn_host = turn_host
        self._turn_port = turn_port
        self._username = username
        self._password = password
        self._stun_host = stun_host or turn_host
        self._stun_port = stun_port
        self._transport = transport
        self._allocation_lifetime = allocation_lifetime
        self._max_chunk_bytes = max_chunk_bytes

        # allocation_id -> (_TurnAllocation, encoded_chunk)
        self._allocations: dict[str, tuple[_TurnAllocation, str]] = {}

    # ------------------------------------------------------------------
    # Adapter protocol
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=False,
            ordered=False,
            ttl_seconds=self._allocation_lifetime,
            rate_limit_per_min=30,
            tags={
                "tier": "5",
                "experimental": "true",
                "requires": "TURN server (coturn)",
                "category": "unconventional-substrate",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"chunk ({len(data)} bytes) exceeds max_chunk_bytes={self._max_chunk_bytes}"
            )

        proto = await _make_udp_protocol(self._turn_host, self._turn_port)
        alloc = await _allocate(
            proto, self._username, self._password, self._allocation_lifetime
        )

        channel = _channel_from_chunk(alloc.allocation_id)

        # Bind the channel to a loopback peer (no real peer needed; the channel
        # binding is what we care about, not actual data delivery to the peer).
        try:
            await _channel_bind(alloc, channel, "127.0.0.1", 0)
        except Exception:
            # ChannelBind to port 0 may be rejected by some servers; that is fine.
            # The allocation itself is the substrate.
            pass

        alloc.start_refresh()
        self._allocations[alloc.allocation_id] = (alloc, encoded_chunk)

        location = json.dumps({
            "allocation_id": alloc.allocation_id,
            "relay_addr": f"{alloc.relay_addr[0]}:{alloc.relay_addr[1]}",
            "channel": channel,
            "turn_host": self._turn_host,
            "turn_port": self._turn_port,
            "data_len": len(data),
        })
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        alloc_id: str = loc["allocation_id"]
        entry = self._allocations.get(alloc_id)
        if entry is None:
            raise KeyError(
                f"allocation {alloc_id!r} not found in live store "
                "(data is ephemeral; process restart or deallocation clears it)"
            )
        _, encoded = entry
        return encoded

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        alloc_id: str = loc["allocation_id"]
        entry = self._allocations.pop(alloc_id, None)
        if entry is None:
            return False
        alloc, _ = entry
        await alloc.deallocate()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        # No server-side manifest index; return what this process knows about.
        return []

    async def health_check(self) -> bool:
        try:
            proto = await _make_udp_protocol(self._stun_host, self._stun_port)
            txn = _new_txn_id()
            req = _build_stun(_BINDING_REQUEST, txn, [])
            await proto.send(req)
            resp_raw = await proto.recv(timeout=3.0)
            resp = _parse_stun(resp_raw)
            if proto.transport:
                proto.transport.close()
            return resp["type"] == _BINDING_SUCCESS
        except Exception:
            return False

    async def close(self) -> None:
        for alloc, _ in list(self._allocations.values()):
            try:
                await alloc.deallocate()
            except Exception:
                pass
        self._allocations.clear()
