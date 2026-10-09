"""FUSE mount — expose PICARD's virtual filesystem via FUSE.

Requires WinFSP on Windows, libfuse on Linux/macOS, or macFUSE.
The `refuse` package (already in pyproject.toml) handles cross-platform binding.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import os
import stat
import threading
import time
from io import BytesIO
from typing import TYPE_CHECKING

try:
    from refuse.high import FUSE, FuseOSError, Operations

    HAS_FUSE = True
except (ImportError, OSError):
    HAS_FUSE = False
    if TYPE_CHECKING:
        from refuse.high import FUSE, FuseOSError, Operations

ENODATA = getattr(errno, "ENODATA", 61)
_XATTR_BIN_PREFIX = "\x00picard:b64:"
_XATTR_BIN_PREFIX_LEGACY = "b64:"

if TYPE_CHECKING:
    from picard.cache import BlockCache
    from picard.index import Index
    from picard.orchestrator import Orchestrator


class _WriteBuffer:
    """Accumulate writes per open file handle, flush on release."""

    __slots__ = ("path", "buf", "mode")

    def __init__(self, path: str, data: bytes = b"", mode: int = 0o100644) -> None:
        self.path = path
        self.buf = BytesIO(data)
        self.mode = mode


class PicardFS(Operations if HAS_FUSE else object):  # type: ignore[misc]
    """FUSE filesystem backed by a PICARD orchestrator."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        passphrase: str,
        *,
        block_cache: BlockCache | None = None,
        readonly: bool = False,
    ) -> None:
        self._orch = orchestrator
        self._index: Index = orchestrator._index
        self._passphrase = passphrase
        self._block_cache = block_cache
        self._readonly = readonly
        self._fh_counter = 0
        self._fh_lock = threading.Lock()
        self._write_buffers: dict[int, _WriteBuffer] = {}
        self._statfs_cache: tuple[dict, float] | None = None
        self._statfs_ttl = 5.0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True,
        )
        self._thread.start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _next_fh(self) -> int:
        with self._fh_lock:
            self._fh_counter += 1
            return self._fh_counter

    def getattr(self, path, fh=None):
        if path == "/":
            now = int(time.time())
            return {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_nlink": 2,
                "st_size": 0,
                "st_ctime": now,
                "st_mtime": now,
                "st_atime": now,
                "st_uid": _getuid(),
                "st_gid": _getgid(),
            }
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        return _manifest_to_stat(m)

    def readdir(self, path, fh):
        yield "."
        yield ".."
        lookup = path if path != "/" else "/"
        for child in self._index.list_children(lookup):
            name = child.rsplit("/", 1)[-1]
            if name:
                yield name

    def read(self, path, size, offset, fh):
        if fh in self._write_buffers:
            wb = self._write_buffers[fh]
            wb.buf.seek(offset)
            return wb.buf.read(size)
        return self._run(
            self._orch.read_range(path, offset, size, self._passphrase)
        )

    def write(self, path, data, offset, fh):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        wb = self._write_buffers.get(fh)
        if wb is None:
            raise FuseOSError(errno.EBADF)
        wb.buf.seek(offset)
        wb.buf.write(data)
        return len(data)

    def create(self, path, mode, fi=None):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        fh = self._next_fh()
        self._write_buffers[fh] = _WriteBuffer(path, mode=mode)
        return fh

    def open(self, path, flags):
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        fh = self._next_fh()
        writing = flags & (os.O_WRONLY | os.O_RDWR)
        if writing and not self._readonly:
            data = self._run(self._orch.get(path, self._passphrase))
            self._write_buffers[fh] = _WriteBuffer(path, data, m.mode)
        return fh

    def release(self, path, fh):
        wb = self._write_buffers.pop(fh, None)
        if wb is not None:
            wb.buf.seek(0)
            data = wb.buf.read()
            self._run(
                self._orch.put(wb.path, data, self._passphrase, mode=wb.mode)
            )
        return 0

    def truncate(self, path, length, fh=None):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        if fh is not None and fh in self._write_buffers:
            wb = self._write_buffers[fh]
            wb.buf.truncate(length)
            return
        if length == 0:
            self._run(self._orch.put(path, b"", self._passphrase))
            return
        data = self._run(
            self._orch.read_range(path, 0, length, self._passphrase)
        )
        self._run(self._orch.put(path, data, self._passphrase))

    def mkdir(self, path, mode):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        self._run(self._orch.mkdir(path, mode=stat.S_IFDIR | mode))

    def rmdir(self, path):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        self._run(self._orch.rmdir(path))

    def unlink(self, path):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        self._run(self._orch.rm(path))

    def rename(self, old, new):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        self._run(self._orch.mv(old, new))

    def symlink(self, target, source):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        self._run(self._orch.symlink(source, target))

    def readlink(self, path):
        return self._run(self._orch.readlink(path))

    def chmod(self, path, mode):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        self._index.update_manifest_metadata(m.manifest_id, mode=mode)

    def chown(self, path, uid, gid):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        self._index.update_manifest_metadata(m.manifest_id, uid=uid, gid=gid)

    def utimens(self, path, times=None):
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        from datetime import datetime, timezone

        if times:
            atime_s, mtime_s = times
            atime = datetime.fromtimestamp(atime_s, tz=timezone.utc).isoformat()
            mtime = datetime.fromtimestamp(mtime_s, tz=timezone.utc).isoformat()
        else:
            now = datetime.now(timezone.utc).isoformat()
            atime = mtime = now
        self._index.update_manifest_metadata(
            m.manifest_id, atime=atime, mtime=mtime,
        )

    def fsync(self, path, datasync, fh):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        wb = self._write_buffers.get(fh)
        if wb is not None:
            wb.buf.seek(0)
            data = wb.buf.read()
            if data:
                self._run(
                    self._orch.put(wb.path, data, self._passphrase, mode=wb.mode)
                )
                wb.buf.seek(0)
                wb.buf.truncate(0)

    def getxattr(self, path, name, position=0):
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        val = m.xattrs.get(name)
        if val is None:
            raise FuseOSError(ENODATA)
        if isinstance(val, str) and val.startswith(_XATTR_BIN_PREFIX):
            return base64.b64decode(val[len(_XATTR_BIN_PREFIX):])
        # Legacy prefix — old code only added it when UTF-8 decode failed,
        # so any stored value with this prefix is genuine binary data.
        if isinstance(val, str) and val.startswith(_XATTR_BIN_PREFIX_LEGACY):
            return base64.b64decode(val[len(_XATTR_BIN_PREFIX_LEGACY):])
        return val.encode("utf-8") if isinstance(val, str) else val

    def setxattr(self, path, name, value, options, position=0):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        xattrs = dict(m.xattrs)
        if isinstance(value, bytes):
            try:
                xattrs[name] = value.decode("utf-8")
            except UnicodeDecodeError:
                xattrs[name] = _XATTR_BIN_PREFIX + base64.b64encode(value).decode("ascii")
        else:
            xattrs[name] = value
        self._index.update_manifest_metadata(m.manifest_id, xattrs=xattrs)

    def listxattr(self, path):
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        return list(m.xattrs.keys())

    def removexattr(self, path, name):
        if self._readonly:
            raise FuseOSError(errno.EROFS)
        m = self._index.get_manifest_by_path(path)
        if m is None:
            raise FuseOSError(errno.ENOENT)
        xattrs = dict(m.xattrs)
        if name not in xattrs:
            raise FuseOSError(ENODATA)
        del xattrs[name]
        self._index.update_manifest_metadata(m.manifest_id, xattrs=xattrs)

    def statfs(self, path):
        now = time.monotonic()
        if self._statfs_cache and (now - self._statfs_cache[1]) < self._statfs_ttl:
            return self._statfs_cache[0]
        du = self._run(self._orch.du())
        result = {
            "f_bsize": 4096,
            "f_frsize": 4096,
            "f_blocks": max(1, du["total_bytes"] // 4096),
            "f_bfree": 2**20,
            "f_bavail": 2**20,
            "f_files": du["total_files"],
            "f_ffree": 2**20,
            "f_favail": 2**20,
            "f_namemax": 255,
        }
        self._statfs_cache = (result, now)
        return result

    def destroy(self, path):
        try:
            if self._block_cache:
                self._block_cache.clear()
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
                self._loop.close()
            finally:
                self._passphrase = ""


def mount(
    orchestrator: Orchestrator,
    passphrase: str,
    mountpoint: str,
    *,
    block_cache: BlockCache | None = None,
    readonly: bool = False,
    foreground: bool = True,
) -> None:
    if not HAS_FUSE:
        raise RuntimeError(
            "FUSE not available — install WinFSP (Windows), "
            "libfuse (Linux), or macFUSE (macOS)"
        )
    fs = PicardFS(
        orchestrator,
        passphrase,
        block_cache=block_cache,
        readonly=readonly,
    )
    FUSE(fs, mountpoint, foreground=foreground, nothreads=False)


def _manifest_to_stat(m) -> dict:
    from datetime import datetime

    mode = m.mode
    if m.file_type == "directory":
        mode = stat.S_IFDIR | (mode & 0o7777)
    elif m.file_type == "symlink":
        mode = stat.S_IFLNK | 0o777
    else:
        mode = stat.S_IFREG | (mode & 0o7777)

    ts = 0.0
    if m.mtime:
        try:
            ts = datetime.fromisoformat(m.mtime).timestamp()
        except (ValueError, OSError):
            pass

    return {
        "st_mode": mode,
        "st_nlink": m.nlink,
        "st_size": m.original_size,
        "st_ctime": ts,
        "st_mtime": ts,
        "st_atime": ts,
        "st_uid": m.uid if hasattr(m, "uid") else _getuid(),
        "st_gid": m.gid if hasattr(m, "gid") else _getgid(),
    }


def _getuid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def _getgid() -> int:
    return getattr(os, "getgid", lambda: 0)()
