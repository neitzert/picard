"""Anonymous pipe adapter -- stores chunks in kernel pipe buffers.

Each chunk lives in an anonymous kernel pipe buffer: a pair of file
descriptors with no filesystem path. The write end is held open; data sits
in the kernel pipe buffer (default 64 KB) until the fds are closed.

Unlike named_pipe_adapter (which uses a filesystem path), this adapter
creates pipes with os.pipe() -- fully anonymous, invisible to ``ls``,
``/proc/mounts``, or any filesystem scanner.

Read is non-destructive: bytes are drained from the read end and immediately
re-written to the write end.

No elevated privileges needed. Cross-platform (fcntl F_SETPIPE_SZ is
Linux-only; gracefully skipped on other platforms).
"""

from __future__ import annotations

import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ADAPTER_NAME = "anon-pipe"

try:
    import fcntl
    _F_SETPIPE_SZ = 1031  # Linux-specific
    _HAS_SETPIPE_SZ = hasattr(os, "pipe") and os.name == "posix"
except ImportError:
    fcntl = None  # type: ignore[assignment]
    _HAS_SETPIPE_SZ = False


class AnonPipeAdapter(Adapter):
    """Store chunks in anonymous kernel pipe buffers."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        # token -> (r_fd, w_fd, encoded_chunk, manifest_id)
        self._pipes: dict[str, tuple[int, int, str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=5000,
            tags={
                "substrate": "anon-pipe",
                "requires": "POSIX",
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max:
            raise ValueError(f"Chunk too large: {len(data)} > {self._max}")
        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        token = uuid.uuid4().hex[:16]
        r_fd, w_fd = os.pipe()

        if fcntl is not None and _HAS_SETPIPE_SZ:
            try:
                fcntl.fcntl(w_fd, _F_SETPIPE_SZ, max(self._max + 64, 65536))
            except OSError:
                pass

        os.write(w_fd, data)
        self._pipes[token] = (r_fd, w_fd, encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"anon-pipe://{token}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = ref.location.removeprefix("anon-pipe://")
        entry = self._pipes.get(token)
        if entry is None:
            raise KeyError(f"anon-pipe chunk not found: {ref.location}")
        r_fd, w_fd, encoded_chunk, _ = entry
        # Read exactly the number of bytes that were written, then re-write so
        # the pipe buffer is restored for subsequent reads.
        data = encoded_chunk.encode()
        n = len(data)
        buf = b""
        remaining = n
        while remaining > 0:
            chunk = os.read(r_fd, remaining)
            if not chunk:
                break
            buf += chunk
            remaining -= len(chunk)
        os.write(w_fd, buf)
        return buf.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = ref.location.removeprefix("anon-pipe://")
        entry = self._pipes.pop(token, None)
        if entry is None:
            return False
        r_fd, w_fd, _, _ = entry
        try:
            os.close(r_fd)
        except OSError:
            pass
        try:
            os.close(w_fd)
        except OSError:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"anon-pipe://{token}")
            for token, (_, _, _, mid) in self._pipes.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        try:
            r, w = os.pipe()
            os.write(w, b"ping")
            result = os.read(r, 4)
            os.close(r)
            os.close(w)
            return result == b"ping"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._pipes):
            ref = StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"anon-pipe://{token}")
            await self.delete_chunk(ref)
