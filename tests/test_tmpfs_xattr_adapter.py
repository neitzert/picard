"""Unit tests for tmpfs_xattr_adapter (Linux-only; patched on other platforms)."""
import sys
import pytest

if sys.platform != "linux":
    pytest.skip("tmpfs_xattr_adapter requires Linux", allow_module_level=True)

from unittest.mock import patch, MagicMock
import asyncio
from picard.adapters.tmpfs_xattr_adapter import TmpfsXattrAdapter
from picard.adapters.base import StoredChunkRef


FAKE_CHUNK = "PCHK:1:testmid:0:0:1:aGVsbG8="


def _adapter():
    with patch("os.open", return_value=3), \
         patch("os.close"), \
         patch("os.makedirs"):
        return TmpfsXattrAdapter()


def test_profile():
    a = _adapter()
    p = a.profile()
    assert p.name == "tmpfs-xattr"
    assert p.persistent is True


def test_write_read_delete():
    a = _adapter()
    with patch("os.setxattr") as mock_set, \
         patch("os.getxattr", return_value=FAKE_CHUNK.encode()) as mock_get, \
         patch("os.removexattr") as mock_rm:
        ref = asyncio.get_event_loop().run_until_complete(a.write_chunk(FAKE_CHUNK))
        assert ref.adapter_name == "tmpfs-xattr"
        assert ref.location.startswith("xattr://")

        data = asyncio.get_event_loop().run_until_complete(a.read_chunk(ref))
        assert data == FAKE_CHUNK

        deleted = asyncio.get_event_loop().run_until_complete(a.delete_chunk(ref))
        assert deleted is True


def test_list_chunks():
    a = _adapter()
    token = "picard_xattr_abc123"
    with patch("os.listxattr", return_value=[f"user.{token}"]), \
         patch("os.getxattr", return_value=FAKE_CHUNK.encode()):
        refs = asyncio.get_event_loop().run_until_complete(a.list_chunks("testmid"))
    assert any(r.adapter_name == "tmpfs-xattr" for r in refs)


def test_health_check():
    a = _adapter()
    with patch("os.getxattr", return_value=b"ok"):
        result = asyncio.get_event_loop().run_until_complete(a.health_check())
    assert result is True
