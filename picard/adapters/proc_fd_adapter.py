"""proc/fd adapter -- stores chunks as open file descriptors in /proc/self/fd/.

Each write_chunk creates a temp file, writes the encoded chunk, holds the
fd open, and unlinks the path so the file exists only via /proc/self/fd/<N>
-> '(deleted)'. Cold reads re-read from the held fd. Visible via
'ls -la /proc/self/fd/' in the process namespace.

Linux/macOS. No root required.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "proc_fd"


class ProcFdAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, str]] = {}  # token -> (fd, encoded_chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "proc-fd-deleted-file"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex[:16]
            # Create temp file, write chunk, unlink path, keep fd open
            fd, path = tempfile.mkstemp(prefix=f"picard.{token[:8]}.")
            try:
                data = encoded_chunk.encode("utf-8")
                os.write(fd, data)
                os.unlink(path)
            except Exception:
                os.close(fd)
                raise
            return token, fd

        token, fd = await asyncio.to_thread(_write)
        self._reg[token] = (fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"proc_fd chunk not in registry: {token}")
        fd, _ = self._reg[token]

        def _read():
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, 65536).decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        token = json.loads(ref.location)["token"]
        entry = self._reg.pop(token, None)
        if entry:
            try:
                os.close(entry[0])
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (_, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            fd, path = tempfile.mkstemp(prefix="picard.hc.")
            os.write(fd, b"test")
            os.unlink(path)
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 4)
            os.close(fd)
            return data == b"test"
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                try:
                    os.close(entry[0])
                except Exception:
                    pass
