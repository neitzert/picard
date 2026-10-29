"""inotify adapter -- stores chunks as inotify file watches.

Each write_chunk creates a temp file and adds it to an inotify watch list
via inotify_init()/inotify_add_watch() using ctypes. The watch descriptor
(WD) and fd are held open; the path appears in /proc/self/fdinfo/<fd>.
Chunk data is held in-process registry.

Linux-only. No root required.
"""
from __future__ import annotations

import sys
if sys.platform != "linux":
    raise ImportError("inotify adapter requires Linux")

import asyncio
import ctypes
import json
import os
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "inotify"
_IN_ACCESS = 0x00000001
_IN_MODIFY  = 0x00000002
_IN_CLOSE   = 0x00000018


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


class InotifyAdapter(Adapter):

    def __init__(self) -> None:
        self._ifd: int | None = None
        self._reg: dict[str, tuple[str, int, str]] = {}  # token -> (path, wd, chunk)

    def _ensure_ifd(self) -> int:
        if self._ifd is None:
            libc = _libc()
            fd = libc.inotify_init1(0o2000000)  # IN_CLOEXEC
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))
            self._ifd = fd
        return self._ifd

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "Linux", "substrate": "inotify-watch-descriptor"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            libc = _libc()
            ifd = self._ensure_ifd()
            token = uuid.uuid4().hex[:16]
            # Create a temp file to watch
            fd, path = tempfile.mkstemp(prefix=f"picard.inot.{token[:8]}.")
            os.close(fd)
            wd = libc.inotify_add_watch(ifd, path.encode(), _IN_ACCESS | _IN_MODIFY)
            if wd < 0:
                err = ctypes.get_errno()
                os.unlink(path)
                raise OSError(err, f"inotify_add_watch: {os.strerror(err)}")
            return token, path, wd

        token, path, wd = await asyncio.to_thread(_write)
        self._reg[token] = (path, wd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "path": path, "wd": wd}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"inotify chunk not in registry: {token}")
        return self._reg[token][2]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        entry = self._reg.pop(token, None)
        if entry:
            path, wd, _ = entry
            if self._ifd is not None:
                try:
                    _libc().inotify_rm_watch(self._ifd, wd)
                except Exception:
                    pass
            if os.path.exists(path):
                os.unlink(path)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk
        refs = []
        for token, (path, wd, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"token": token,
                                                "path": path, "wd": wd})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            libc = _libc()
            fd = libc.inotify_init1(0o2000000)
            if fd < 0:
                return False
            os.close(fd)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        for token in list(self._reg):
            entry = self._reg.pop(token, None)
            if entry:
                path, wd, _ = entry
                if self._ifd is not None:
                    try:
                        _libc().inotify_rm_watch(self._ifd, wd)
                    except Exception:
                        pass
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
        if self._ifd is not None:
            try:
                os.close(self._ifd)
            except Exception:
                pass
            self._ifd = None
