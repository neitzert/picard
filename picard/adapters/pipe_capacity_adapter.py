"""Pipe capacity adapter -- stores chunks in anonymous Linux pipe buffers.

Each write_chunk calls os.pipe(), sets the pipe capacity via fcntl F_SETPIPE_SZ,
and writes the encoded chunk into the write end. Data sits in the kernel pipe
buffer. read_chunk reads and immediately rewrites to leave data in place.

Linux/macOS. No root required.
"""
from __future__ import annotations

import sys
import asyncio
import fcntl
import json
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "pipe_capacity"
_F_SETPIPE_SZ = 1031
_F_GETPIPE_SZ = 1032


class PipeCapacityAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, int, str]] = {}  # token -> (r_fd, w_fd, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "pipe-kernel-buffer"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            r_fd, w_fd = os.pipe()
            data = encoded_chunk.encode("utf-8")
            # Set pipe size large enough to hold the data (rounded up to page size)
            if sys.platform == "linux":
                needed = max(4096, len(data) + 512)
                try:
                    fcntl.fcntl(w_fd, _F_SETPIPE_SZ, needed)
                except OSError:
                    pass
            os.write(w_fd, data)
            return token, r_fd, w_fd

        token, r_fd, w_fd = await asyncio.to_thread(_write)
        self._reg[token] = (r_fd, w_fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"pipe chunk not in registry: {token}")
        r_fd, w_fd, _ = self._reg[token]

        def _read():
            data = os.read(r_fd, 65536)
            os.write(w_fd, data)
            return data.decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            for fd in (entry[0], entry[1]):
                try:
                    os.close(fd)
                except Exception:
                    pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, _, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            r, w = os.pipe()
            os.write(w, b"test")
            result = os.read(r, 4)
            os.close(r)
            os.close(w)
            return result == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                for fd in (entry[0], entry[1]):
                    try:
                        os.close(fd)
                    except Exception:
                        pass
