"""TTY window size adapter -- stores chunks in pty terminal window size.

Each write_chunk creates a pty pair (os.openpty()), encodes part of the
chunk hash into the rows/cols fields via TIOCSWINSZ ioctl, and holds the
master fd open. The window size is visible via TIOCGWINSZ. Chunk data is
held in-process registry.

Linux/macOS. No root required.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import struct
import termios
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "tty_winsize"

try:
    _TIOCSWINSZ = termios.TIOCSWINSZ if hasattr(termios, "TIOCSWINSZ") else 0x5414
    _TIOCGWINSZ = termios.TIOCGWINSZ if hasattr(termios, "TIOCGWINSZ") else 0x5413
except AttributeError:
    _TIOCSWINSZ = 0x5414
    _TIOCGWINSZ = 0x5413


class TtyWinsizeAdapter(Adapter):

    def __init__(self) -> None:
        self._reg: dict[str, tuple[int, int, str]] = {}  # token -> (master_fd, slave_fd, chunk)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=False,
            ordered=False,
            tags={"requires": "pty", "substrate": "tty-window-size-ioctl"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            import hashlib
            token = uuid.uuid4().hex[:16]
            master_fd, slave_fd = os.openpty()
            # Encode first 8 bytes of sha256(chunk) as rows/cols/xpix/ypix (4 x uint16)
            h = hashlib.sha256(encoded_chunk.encode()).digest()[:8]
            rows, cols, xpix, ypix = struct.unpack(">HHHH", h)
            winsize = struct.pack("HHHH", rows, cols, xpix, ypix)
            fcntl.ioctl(slave_fd, _TIOCSWINSZ, winsize)
            return token, master_fd, slave_fd

        token, master_fd, slave_fd = await asyncio.to_thread(_write)
        self._reg[token] = (master_fd, slave_fd, encoded_chunk)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        token = json.loads(ref.location)["token"]
        if token not in self._reg:
            raise KeyError(f"tty_winsize chunk not in registry: {token}")
        return self._reg[token][2]

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
            master_fd, slave_fd = os.openpty()
            winsize = struct.pack("HHHH", 24, 80, 0, 0)
            fcntl.ioctl(slave_fd, _TIOCSWINSZ, winsize)
            result = fcntl.ioctl(slave_fd, _TIOCGWINSZ, b"\x00" * 8)
            rows, cols = struct.unpack("HH", result[:4])
            os.close(master_fd); os.close(slave_fd)
            return rows == 24 and cols == 80
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
