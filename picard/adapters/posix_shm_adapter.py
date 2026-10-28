"""POSIX shared memory adapter -- stores chunks in named POSIX shm segments.

Each chunk lives in a /dev/shm/picard.<token> virtual file backed by the
kernel's tmpfs. Unlike tmpfs_xattr_adapter (which uses xattrs on one file),
this adapter creates one shm object per chunk. Objects persist until
shm_unlink or reboot.

Format: 4-byte big-endian length prefix followed by the encoded chunk bytes.

Requires: Linux or macOS. No elevated privileges needed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

if sys.platform not in ("linux", "darwin"):
    raise ImportError("posix_shm_adapter requires Linux or macOS")

_ADAPTER_NAME = "posix-shm"

if sys.platform == "linux":
    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
else:
    _libc = ctypes.CDLL("libc.dylib", use_errno=True)

_O_CREAT = 0o100
_O_RDWR = 0o2
_PROT_READ = 1
_PROT_WRITE = 2
_MAP_SHARED = 1
_MAP_FAILED = ctypes.c_void_p(-1).value

_libc.shm_open.restype = ctypes.c_int
_libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
_libc.shm_unlink.restype = ctypes.c_int
_libc.shm_unlink.argtypes = [ctypes.c_char_p]
_libc.ftruncate.restype = ctypes.c_int
_libc.ftruncate.argtypes = [ctypes.c_int, ctypes.c_long]
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
_libc.munmap.restype = ctypes.c_int
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.close.restype = ctypes.c_int
_libc.close.argtypes = [ctypes.c_int]


def _shm_write(name: bytes, data: bytes) -> None:
    size = 4 + len(data)
    fd = _libc.shm_open(name, _O_CREAT | _O_RDWR, 0o600)
    if fd < 0:
        raise OSError(ctypes.get_errno(), f"shm_open failed for {name!r}")
    try:
        if _libc.ftruncate(fd, size) != 0:
            raise OSError(ctypes.get_errno(), "ftruncate failed")
        ptr = _libc.mmap(None, size, _PROT_READ | _PROT_WRITE, _MAP_SHARED, fd, 0)
        if ptr == _MAP_FAILED or ptr is None:
            raise OSError(ctypes.get_errno(), "mmap failed")
        try:
            buf = (ctypes.c_char * size).from_address(ptr)
            buf[:4] = len(data).to_bytes(4, "big")
            buf[4:size] = data
        finally:
            _libc.munmap(ptr, size)
    finally:
        _libc.close(fd)


def _shm_read(name: bytes) -> bytes:
    fd = _libc.shm_open(name, _O_RDWR, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), f"shm_open failed for {name!r}")
    try:
        hdr_ptr = _libc.mmap(None, 4, _PROT_READ, _MAP_SHARED, fd, 0)
        if hdr_ptr == _MAP_FAILED or hdr_ptr is None:
            raise OSError(ctypes.get_errno(), "mmap header failed")
        try:
            hdr = bytes((ctypes.c_char * 4).from_address(hdr_ptr))
            data_len = int.from_bytes(hdr, "big")
        finally:
            _libc.munmap(hdr_ptr, 4)
        size = 4 + data_len
        ptr = _libc.mmap(None, size, _PROT_READ, _MAP_SHARED, fd, 0)
        if ptr == _MAP_FAILED or ptr is None:
            raise OSError(ctypes.get_errno(), "mmap failed")
        try:
            return bytes((ctypes.c_char * data_len).from_address(ptr + 4))
        finally:
            _libc.munmap(ptr, size)
    finally:
        _libc.close(fd)


class PosixShmAdapter(Adapter):
    """Store chunks in POSIX named shared memory segments."""

    def __init__(self, *, max_chunk_bytes: int = 65536) -> None:
        self._max = max_chunk_bytes
        self._registry: dict[bytes, tuple[str, str]] = {}  # name -> (encoded_chunk, manifest_id)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_ADAPTER_NAME,
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=1000,
            tags={
                "substrate": "posix-shm",
                "requires": "Linux or macOS",
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
        name = b"/picard." + token.encode()
        _shm_write(name, data)
        self._registry[name] = (encoded_chunk, manifest_id)
        return StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"posix-shm://{name.decode()}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        name_str = ref.location.removeprefix("posix-shm://")
        name = name_str.encode()
        if name not in self._registry:
            raise KeyError(f"posix-shm chunk not found: {ref.location}")
        try:
            return _shm_read(name).decode()
        except OSError as exc:
            raise KeyError(f"posix-shm chunk not found: {ref.location}") from exc

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        name_str = ref.location.removeprefix("posix-shm://")
        name = name_str.encode()
        if name not in self._registry:
            return False
        _libc.shm_unlink(name)
        del self._registry[name]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name=_ADAPTER_NAME, location=f"posix-shm://{name.decode()}")
            for name, (_, mid) in self._registry.items()
            if mid == manifest_id
        ]

    async def health_check(self) -> bool:
        shm_dir = "/dev/shm" if sys.platform == "linux" else "/tmp"
        return os.path.isdir(shm_dir)

    async def close(self) -> None:
        pass
