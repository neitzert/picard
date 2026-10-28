"""Abstract Unix socket adapter -- stores chunks as kernel socket table entries.

Abstract Unix domain sockets (paths beginning with a null byte) live exclusively
in the Linux kernel socket table. They have no filesystem path, are invisible to
``ls`` and ``find``, and appear only in ``/proc/net/unix`` as ``@picard.<token>``
entries. Data is stored in-process; the bound socket is the visible commitment.

Each chunk spawns one daemon thread that holds the socket bound so that the kernel
entry persists. On ``delete_chunk`` or ``close`` the socket is released and the
kernel entry vanishes immediately.

Requires: Linux. No elevated privileges needed.
"""

from __future__ import annotations

import socket
import sys
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("abstract_socket_adapter requires Linux")

_ADAPTER_NAME = "abstract-socket"


class AbstractSocketAdapter(Adapter):
    """Scatter chunks into the Linux abstract Unix socket namespace."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # token -> (sock, stop_event, encoded_chunk, manifest_id)
        self._sockets: dict[str, tuple[socket.socket, threading.Event, str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={
                "substrate": "abstract-unix-socket",
                "requires": "Linux",
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
        abstract_path = b"\x00picard." + token.encode()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(abstract_path)
        sock.listen(1)

        stop_event = threading.Event()

        def _hold() -> None:
            stop_event.wait()

        t = threading.Thread(target=_hold, daemon=True)
        t.start()

        self._sockets[token] = (sock, stop_event, encoded_chunk, manifest_id)
        location = f"abstract-socket://{token}"
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("abstract-socket://")
        entry = self._sockets.get(token)
        if entry is None:
            raise KeyError(f"abstract-socket chunk not found: {ref.location}")
        return entry[2]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("abstract-socket://")
        entry = self._sockets.pop(token, None)
        if entry is None:
            return False
        sock, stop_event, _, _ = entry
        stop_event.set()
        try:
            sock.close()
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"abstract-socket://{token}")
            for token, (_, _, _, mid) in self._sockets.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(b"\x00picard.healthcheck")
            s.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._sockets):
            sock, stop_event, _, _ = self._sockets.pop(token)
            stop_event.set()
            try:
                sock.close()
            except Exception:
                pass
