"""prctl name adapter -- stores chunks as Linux thread names.

Each chunk spawns one daemon thread whose name is set via ``prctl(PR_SET_NAME)``
to ``picard.<token8>`` (15 bytes, the kernel maximum). The thread name is visible
in ``ps aux``, ``top``, ``htop``, and ``/proc/<pid>/task/<tid>/comm``. Data is
stored in-process; the thread name is the visible commitment in the process table.

On ``delete_chunk`` or ``close``, the daemon thread is stopped and the thread name
disappears from the process table.

Requires: Linux. No elevated privileges needed.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("prctl_name_adapter requires Linux")

_ADAPTER_NAME = "prctl-name"

PR_SET_NAME = 15
_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def _set_thread_name(name: bytes) -> None:
    # Truncate to 15 bytes + null terminator (16 total, kernel silently truncates anyway)
    _libc.prctl(PR_SET_NAME, name[:15] + b"\x00", 0, 0, 0)


class PrctlNameAdapter(Adapter):
    """Scatter chunks into Linux thread name slots via prctl(PR_SET_NAME)."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # token -> (thread, stop_event, encoded_chunk, manifest_id)
        self._threads: dict[str, tuple[threading.Thread, threading.Event, str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=500,
            tags={
                "substrate": "prctl-thread-name",
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

        token = uuid.uuid4().hex[:12]
        stop_event = threading.Event()

        def _hold() -> None:
            _set_thread_name(b"picard." + token[:8].encode())
            stop_event.wait()

        t = threading.Thread(target=_hold, daemon=True)
        t.start()

        self._threads[token] = (t, stop_event, encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"prctl-name://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("prctl-name://")
        entry = self._threads.get(token)
        if entry is None:
            raise KeyError(f"prctl-name chunk not found: {ref.location}")
        return entry[2]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("prctl-name://")
        entry = self._threads.pop(token, None)
        if entry is None:
            return False
        _, stop_event, _, _ = entry
        stop_event.set()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"prctl-name://{token}")
            for token, (_, _, _, mid) in self._threads.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            ret = _libc.prctl(PR_SET_NAME, b"picard.health\x00", 0, 0, 0)
            return ret == 0
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._threads):
            _, stop_event, _, _ = self._threads.pop(token)
            stop_event.set()
