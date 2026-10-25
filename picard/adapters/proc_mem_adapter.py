"""Process memory adapter -- stores chunks in a companion process's address space.

This adapter exploits the fact that anonymous memory-backed file descriptors
(memfd_create on Linux) have no path in the filesystem and are not visible to
standard monitoring tools. Chunks live as open file descriptors held by a
companion daemon process. No filesystem entry, no named socket, no block
device -- the data exists only as mapped anonymous memory in another process.

Two modes:

  memfd   (Linux, recommended)
          The adapter creates a memfd, writes the chunk, then passes the file
          descriptor to a long-lived companion daemon via SCM_RIGHTS over a
          Unix domain socket. The companion holds the fd open indefinitely.
          Data persists until the companion exits or the chunk is deleted.
          Requires: Linux, running companion daemon at companion_socket.

  self    (cross-platform fallback)
          The adapter allocates anonymous mmap() regions inside its own
          process. No companion needed. Data lives only for the lifetime of
          this adapter instance (i.e., the PICARD process). Single-process
          only.

Companion daemon:
    python -m picard.proc_mem_companion --socket /tmp/picard-procmem.sock

EXPERIMENTAL: This adapter is a research substrate. Data is lost when the
companion daemon exits. It is not suitable as a primary storage backend.

WARNING: The memfd mode works only on Linux. The self mode works everywhere
but provides no durability beyond the current process.
"""

from __future__ import annotations

import array
import asyncio
import json
import mmap
import os
import secrets
import socket
import sys
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

# ---------------------------------------------------------------------------
# Low-level primitives (Linux only)
# ---------------------------------------------------------------------------

_IS_LINUX = sys.platform == "linux"
_MFD_CLOEXEC = 1

if _IS_LINUX:
    import ctypes as _ctypes
    _libc = _ctypes.CDLL(None, use_errno=True)


def _memfd_create(name: str, size: int) -> int:
    """Create an anonymous RAM-backed fd and size it.  Linux only."""
    if not _IS_LINUX:
        raise NotImplementedError("memfd_create is Linux-only; use mode='self'")
    fd = _libc.memfd_create(name.encode(), _MFD_CLOEXEC)
    if fd < 0:
        import ctypes
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    os.ftruncate(fd, size)
    return fd


def _send_fd(sock: socket.socket, fd: int, msg: bytes = b"\x00") -> None:
    """Send an fd to another process via SCM_RIGHTS."""
    fds = array.array("i", [fd])
    sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])


def _recv_fd(sock: socket.socket) -> tuple[bytes, int]:
    """Receive an fd sent via SCM_RIGHTS.  Returns (msg, fd)."""
    fds = array.array("i")
    msg, ancdata, _, _ = sock.recvmsg(256, socket.CMSG_LEN(fds.itemsize))
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            fds.frombytes(
                cmsg_data[: len(cmsg_data) - (len(cmsg_data) % fds.itemsize)]
            )
    return msg, fds[0] if len(fds) > 0 else -1


# ---------------------------------------------------------------------------
# Self-mode in-process store
# ---------------------------------------------------------------------------

class _SelfStore:
    """Stores chunks as anonymous mmap regions inside this process."""

    def __init__(self) -> None:
        self._store: dict[str, mmap.mmap] = {}

    def write(self, token: str, data: bytes) -> None:
        m = mmap.mmap(-1, len(data))
        m.write(data)
        self._store[token] = m

    def read(self, token: str) -> bytes:
        if token not in self._store:
            raise KeyError(token)
        m = self._store[token]
        m.seek(0)
        return m.read()

    def delete(self, token: str) -> bool:
        m = self._store.pop(token, None)
        if m is not None:
            m.close()
            return True
        return False

    def list_tokens(self) -> list[str]:
        return list(self._store.keys())

    def close(self) -> None:
        for m in self._store.values():
            m.close()
        self._store.clear()


# ---------------------------------------------------------------------------
# Companion socket client
# ---------------------------------------------------------------------------

class _CompanionClient:
    """Synchronous client for the proc_mem_companion daemon."""

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self._path)
        self._sock = s
        return s

    def _rpc(self, request: dict) -> dict:
        s = self._connect()
        s.sendall(json.dumps(request).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("Companion closed connection")
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])

    def ping(self) -> bool:
        try:
            resp = self._rpc({"op": "ping"})
            return bool(resp.get("ok"))
        except Exception:
            return False

    def write(self, token: str, data: bytes) -> None:
        if not _IS_LINUX:
            raise NotImplementedError("memfd mode requires Linux")
        fd = _memfd_create("picard-chunk", len(data))
        try:
            os.write(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            resp = self._rpc({"op": "write", "token": token, "size": len(data)})
            if not resp.get("ok"):
                raise RuntimeError(f"Companion write failed: {resp}")
            _send_fd(self._sock, fd)  # type: ignore[arg-type]
        finally:
            os.close(fd)

    def read(self, token: str) -> bytes:
        if not _IS_LINUX:
            raise NotImplementedError("memfd mode requires Linux")
        resp = self._rpc({"op": "read", "token": token})
        if not resp.get("ok"):
            raise KeyError(f"Token not found: {token}")
        _, fd = _recv_fd(self._sock)  # type: ignore[arg-type]
        try:
            size = resp["size"]
            return os.read(fd, size)
        finally:
            os.close(fd)

    def delete(self, token: str) -> bool:
        resp = self._rpc({"op": "delete", "token": token})
        return bool(resp.get("ok")) and resp.get("existed", True)

    def list_tokens(self, manifest_id: str) -> list[str]:
        resp = self._rpc({"op": "list", "manifest_id": manifest_id})
        return resp.get("tokens", [])

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ProcMemAdapter(Adapter):
    """Stores chunks in a companion process's anonymous address space.

    In memfd mode the data has no filesystem path and no inode number that
    survives a directory listing -- it is invisible to standard forensic
    tools that enumerate files, sockets, and named objects.

    Mode 'memfd' is Linux-only and requires the companion daemon to be
    running at companion_socket:

        python -m picard.proc_mem_companion \\
            --socket /tmp/picard-procmem.sock

    Mode 'self' works on all platforms but stores data inside the current
    process, so it is lost when the adapter is closed.
    """

    def __init__(
        self,
        mode: str = "memfd",
        companion_socket: str = "/tmp/picard-procmem.sock",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        if mode not in ("memfd", "self"):
            raise ValueError(f"Unknown mode {mode!r}; use 'memfd' or 'self'")
        if mode == "memfd" and not _IS_LINUX:
            raise NotImplementedError(
                "proc-mem adapter mode='memfd' requires Linux. "
                "Use mode='self' on this platform."
            )
        self._mode = mode
        self._socket_path = companion_socket
        self._max_chunk_bytes = max_chunk_bytes

        # token -> (manifest_id, data_length)
        self._registry: dict[str, tuple[str, int]] = {}

        if mode == "self":
            self._store: _SelfStore | None = _SelfStore()
            self._client: _CompanionClient | None = None
        else:
            self._store = None
            self._client = _CompanionClient(companion_socket)

    # ------------------------------------------------------------------
    # Adapter interface
    # ------------------------------------------------------------------

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="proc-mem",
            max_chunk_bytes=self._max_chunk_bytes,
            persistent=False,
            ordered=True,
            tags={
                "tier": "4",
                "experimental": "true",
                "mode": self._mode,
            },
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data = encoded_chunk.encode()
        if len(data) > self._max_chunk_bytes:
            raise ValueError(
                f"Chunk {len(data)} bytes exceeds max {self._max_chunk_bytes}"
            )
        token = secrets.token_hex(16)
        manifest_id = ""

        if self._mode == "self":
            assert self._store is not None
            await asyncio.get_event_loop().run_in_executor(
                None, self._store.write, token, data
            )
        else:
            assert self._client is not None
            await asyncio.get_event_loop().run_in_executor(
                None, self._client.write, token, data
            )

        self._registry[token] = (manifest_id, len(data))
        location = json.dumps({"token": token, "size": len(data), "mode": self._mode})
        return StoredChunkRef(adapter_name="proc-mem", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        size = loc["size"]

        if self._mode == "self":
            assert self._store is not None
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._store.read, token
            )
        else:
            assert self._client is not None
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._client.read, token
            )

        return data[:size].decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        self._registry.pop(token, None)

        if self._mode == "self":
            assert self._store is not None
            return await asyncio.get_event_loop().run_in_executor(
                None, self._store.delete, token
            )
        else:
            assert self._client is not None
            return await asyncio.get_event_loop().run_in_executor(
                None, self._client.delete, token
            )

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, (mid, size) in self._registry.items():
            if mid == manifest_id:
                location = json.dumps(
                    {"token": token, "size": size, "mode": self._mode}
                )
                refs.append(StoredChunkRef(adapter_name="proc-mem", location=location))
        return refs

    async def health_check(self) -> bool:
        if self._mode == "self":
            return self._store is not None
        assert self._client is not None
        return await asyncio.get_event_loop().run_in_executor(
            None, self._client.ping
        )

    async def close(self) -> None:
        if self._store is not None:
            self._store.close()
        if self._client is not None:
            self._client.close()
