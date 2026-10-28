"""Netlink socket adapter -- stores chunks as kernel socket table entries.

Each chunk binds an AF_NETLINK/NETLINK_USERSOCK socket to multicast group 1.
The socket is visible in /proc/net/netlink. Data lives in the in-process
registry; the bound socket is the kernel-visible commitment. Optionally
broadcasts a netlink message (fire-and-forget) to group 1.

Eviction: sockets are closed on delete_chunk or adapter.close().

Requires: Linux. No elevated privileges needed for NETLINK_USERSOCK.
"""

from __future__ import annotations

import socket
import struct
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("netlink_socket_adapter requires Linux")

_ADAPTER_NAME = "netlink-socket"

_AF_NETLINK = 16
_NETLINK_USERSOCK = 2
_NLMSG_DONE = 3


def _build_nlmsg(payload: bytes) -> bytes:
    """Build a minimal netlink message with NLMSG_DONE type."""
    length = 16 + len(payload)
    return struct.pack("<IHHII", length, _NLMSG_DONE, 0, 0, 0) + payload


class NetlinkSocketAdapter(Adapter):
    """Store chunks with AF_NETLINK sockets bound to multicast group 1."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # token -> (sock, encoded_chunk, manifest_id)
        self._sockets: dict[str, tuple[socket.socket, str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={
                "substrate": "netlink-socket",
                "requires": "Linux",
                "note": "data in in-process registry; socket visible in /proc/net/netlink",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        if len(encoded_chunk.encode()) > self._max:
            raise ValueError(
                f"Chunk too large: {len(encoded_chunk.encode())} > {self._max}"
            )
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        sock = socket.socket(_AF_NETLINK, socket.SOCK_RAW, _NETLINK_USERSOCK)
        sock.bind((0, 1))

        # Fire-and-forget broadcast (best-effort)
        try:
            msg = _build_nlmsg(encoded_chunk.encode()[:self._max])
            sock.sendto(msg, (0, 1))
        except OSError:
            pass

        self._sockets[token] = (sock, encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"netlink://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("netlink://")
        entry = self._sockets.get(token)
        if entry is None:
            raise KeyError(f"netlink-socket chunk not found: {ref.location}")
        return entry[1]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("netlink://")
        entry = self._sockets.pop(token, None)
        if entry is None:
            return False
        sock, _, _ = entry
        try:
            sock.close()
        except OSError:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"netlink://{token}")
            for token, (_, _, mid) in self._sockets.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            s = socket.socket(_AF_NETLINK, socket.SOCK_RAW, _NETLINK_USERSOCK)
            s.close()
            return True
        except OSError:
            return False

    async def close(self) -> None:
        for token in list(self._sockets):
            ref = StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"netlink://{token}")
            await self.delete_chunk(ref)
