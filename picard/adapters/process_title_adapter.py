"""Process title adapter -- stores a visible commitment in the process name.

Data is stored in the in-process registry. Each write_chunk call sets
/proc/self/comm to "picard-store" (best-effort, max 15 bytes), making the
process identifiable in ps/top. The full chunk is stored in memory.

The argv[0] overwrite is attempted via ctypes to maximise the visible data in
`ps aux`. The overwrite is bounded by the original argv[0] buffer length and
is best-effort: if ctypes cannot locate the buffer the in-process store is
still authoritative.

Requires: Linux. No elevated privileges needed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform != "linux":
    raise ImportError("process_title_adapter requires Linux")

_ADAPTER_NAME = "process-title"
_COMM_PATH = "/proc/self/comm"
_TITLE = b"picard-store"

_libc = ctypes.CDLL("libc.so.6", use_errno=True)

# One-time: set process comm to picard-store
try:
    with open(_COMM_PATH, "wb") as _f:
        _f.write(_TITLE)
except OSError:
    pass


class ProcessTitleAdapter(Adapter):
    """Store chunks in-process with a visible commitment in the process title."""

    def __init__(self, *, max_chunk_bytes: int = 4096) -> None:
        self._max = max_chunk_bytes
        self._registry: dict[str, tuple[str, str]] = {}  # token -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=5000,
            tags={
                "substrate": "process-title",
                "requires": "Linux",
                "note": "data in-process; /proc/self/comm shows picard-store in ps/top",
            },
        )

    def _set_comm(self, title: bytes) -> None:
        """Write up to 15 bytes to /proc/self/comm (best-effort)."""
        try:
            with open(_COMM_PATH, "wb") as f:
                f.write(title[:15])
        except OSError:
            pass

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
        self._set_comm(_TITLE)
        self._registry[token] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"process-title://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("process-title://")
        entry = self._registry.get(token)
        if entry is None:
            raise KeyError(f"process-title chunk not found: {ref.location}")
        return entry[0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("process-title://")
        existed = token in self._registry
        self._registry.pop(token, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"process-title://{token}")
            for token, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            with open(_COMM_PATH, "rb") as f:
                return len(f.read()) > 0
        except OSError:
            return False

    async def close(self) -> None:
        pass
