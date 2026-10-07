"""Tests for FUSE mount — unit test PicardFS methods directly."""

import errno
import stat

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.fuse_mount import HAS_FUSE, PicardFS
from picard.index import Index
from picard.orchestrator import Orchestrator

pytestmark = pytest.mark.skipif(not HAS_FUSE, reason="FUSE libs not available")


@pytest.fixture
def fs():
    adapter = MemoryAdapter(max_chunk_bytes=256)
    index = Index()
    orch = Orchestrator([adapter], index, block_size=128)
    pfs = PicardFS(orch, "pw")
    yield pfs
    pfs.destroy("/")


class TestGetattr:
    def test_root(self, fs):
        st = fs.getattr("/")
        assert st["st_mode"] & stat.S_IFDIR

    def test_file(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        st = fs.getattr("/test.txt")
        assert st["st_mode"] & stat.S_IFREG
        assert st["st_size"] == 5

    def test_nonexistent(self, fs):
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            fs.getattr("/nope")


class TestReaddir:
    def test_root_empty(self, fs):
        entries = list(fs.readdir("/", None))
        assert "." in entries
        assert ".." in entries

    def test_with_files(self, fs):
        fs._run(fs._orch.put("/a.txt", b"a", "pw"))
        entries = list(fs.readdir("/", None))
        assert "a.txt" in entries


class TestReadWrite:
    def test_read(self, fs):
        fs._run(fs._orch.put("/data.txt", b"hello world", "pw"))
        fh = fs.open("/data.txt", 0)
        result = fs.read("/data.txt", 5, 0, fh)
        assert result == b"hello"
        fs.release("/data.txt", fh)

    def test_read_offset(self, fs):
        fs._run(fs._orch.put("/data.txt", b"hello world", "pw"))
        fh = fs.open("/data.txt", 0)
        result = fs.read("/data.txt", 5, 6, fh)
        assert result == b"world"
        fs.release("/data.txt", fh)

    def test_create_and_write(self, fs):
        fh = fs.create("/new.txt", 0o644)
        fs.write("/new.txt", b"content", 0, fh)
        fs.release("/new.txt", fh)
        data = fs._run(fs._orch.get("/new.txt", "pw"))
        assert data == b"content"


class TestMkdirRmdir:
    def test_mkdir(self, fs):
        fs.mkdir("/mydir", 0o755)
        st = fs.getattr("/mydir")
        assert st["st_mode"] & stat.S_IFDIR

    def test_rmdir(self, fs):
        fs.mkdir("/mydir", 0o755)
        fs.rmdir("/mydir")
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            fs.getattr("/mydir")


class TestSymlinkReadlink:
    def test_symlink(self, fs):
        fs._run(fs._orch.put("/target.txt", b"data", "pw"))
        fs.symlink("/target.txt", "/link.txt")
        result = fs.readlink("/link.txt")
        assert result == "/target.txt"


class TestUnlink:
    def test_unlink(self, fs):
        fs._run(fs._orch.put("/bye.txt", b"gone", "pw"))
        fs.unlink("/bye.txt")
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            fs.getattr("/bye.txt")


class TestRename:
    def test_rename(self, fs):
        fs._run(fs._orch.put("/old.txt", b"data", "pw"))
        fs.rename("/old.txt", "/new.txt")
        st = fs.getattr("/new.txt")
        assert st["st_size"] == 4


class TestStatfs:
    def test_statfs(self, fs):
        result = fs.statfs("/")
        assert "f_bsize" in result
        assert "f_namemax" in result


class TestFuseXattr:
    def test_setxattr_getxattr_roundtrip(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        fs.setxattr("/test.txt", "user.tag", b"important", 0)
        val = fs.getxattr("/test.txt", "user.tag")
        assert val == b"important"

    def test_listxattr_empty(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        result = fs.listxattr("/test.txt")
        assert result == []

    def test_listxattr_with_attrs(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        fs.setxattr("/test.txt", "user.a", b"1", 0)
        fs.setxattr("/test.txt", "user.b", b"2", 0)
        result = fs.listxattr("/test.txt")
        assert sorted(result) == ["user.a", "user.b"]

    def test_removexattr(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        fs.setxattr("/test.txt", "user.tag", b"val", 0)
        fs.removexattr("/test.txt", "user.tag")
        assert fs.listxattr("/test.txt") == []

    def test_getxattr_missing_raises(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            fs.getxattr("/test.txt", "user.nonexistent")

    def test_removexattr_missing_raises(self, fs):
        fs._run(fs._orch.put("/test.txt", b"hello", "pw"))
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            fs.removexattr("/test.txt", "user.nonexistent")


class TestFuseFsync:
    def test_fsync_flushes_write_buffer(self, fs):
        fh = fs.create("/sync.txt", 0o100644)
        fs.write("/sync.txt", b"synced data", 0, fh)
        fs.fsync("/sync.txt", False, fh)
        m = fs._orch._index.get_manifest_by_path("/sync.txt")
        assert m is not None
        fs.release("/sync.txt", fh)

    def test_fsync_noop_without_buffer(self, fs):
        fs.fsync("/nonexistent.txt", False, 9999)


class TestReadonly:
    def test_write_blocked(self):
        adapter = MemoryAdapter(max_chunk_bytes=256)
        index = Index()
        orch = Orchestrator([adapter], index, block_size=128)
        pfs = PicardFS(orch, "pw", readonly=True)
        from refuse.high import FuseOSError

        with pytest.raises(FuseOSError):
            pfs.create("/x.txt", 0o644)
        pfs.destroy("/")
