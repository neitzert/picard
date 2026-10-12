"""Tests for picard.webdav_server — WebDAV provider over the orchestrator."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.manifest import Manifest
from picard.webdav_server import (
    PicardDirectory,
    PicardFile,
    PicardProvider,
    _parse_iso,
)


# ── Fixtures ──

def _make_manifest(
    path: str = "/hello.txt",
    file_type: str = "regular",
    size: int = 13,
    mtime: str = "2026-01-01T00:00:00+00:00",
) -> Manifest:
    return Manifest(
        manifest_id="deadbeef",
        path=path,
        original_size=size,
        block_size=65536,
        total_blocks=1 if size else 0,
        created_at=mtime,
        file_type=file_type,
        mode=0o100644 if file_type == "regular" else 0o040755,
        mtime=mtime,
        ctime=mtime,
        atime=mtime,
        version=1,
        parent_path="/",
    )


@pytest.fixture
def mock_orch():
    orch = MagicMock()
    orch.get = AsyncMock(return_value=b"Hello, world!")
    orch.put = AsyncMock(return_value=_make_manifest())
    orch.rm = AsyncMock(return_value=True)
    orch.mkdir = AsyncMock(return_value=_make_manifest("/docs", "directory", 0))
    orch.mv = AsyncMock(return_value=True)
    orch.cp = AsyncMock(return_value=True)
    orch.close = AsyncMock()
    return orch


@pytest.fixture
def mock_index():
    index = MagicMock()
    index.list_children = MagicMock(return_value=["/hello.txt", "/docs"])
    index.get_manifest_by_path = MagicMock(side_effect=lambda p: {
        "/": _make_manifest("/", "directory", 0),
        "/hello.txt": _make_manifest("/hello.txt", "regular", 13),
        "/docs": _make_manifest("/docs", "directory", 0),
    }.get(p))
    return index


@pytest.fixture
def provider(mock_orch, mock_index):
    return PicardProvider(mock_orch, mock_index, passphrase="test", read_only=False)


@pytest.fixture
def ro_provider(mock_orch, mock_index):
    return PicardProvider(mock_orch, mock_index, passphrase="test", read_only=True)


def _make_environ(provider=None):
    return {"REQUEST_METHOD": "GET", "wsgidav.provider": provider}


# ── Provider tests ──

class TestProvider:
    def test_root_is_directory(self, provider):
        res = provider.get_resource_inst("/", _make_environ(provider))
        assert isinstance(res, PicardDirectory)

    def test_file_resource(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        assert isinstance(res, PicardFile)

    def test_dir_resource(self, provider):
        res = provider.get_resource_inst("/docs", _make_environ(provider))
        assert isinstance(res, PicardDirectory)

    def test_missing_resource(self, provider, mock_index):
        mock_index.get_manifest_by_path = MagicMock(return_value=None)
        res = provider.get_resource_inst("/nope.txt", _make_environ(provider))
        assert res is None

    def test_readonly_flag(self, ro_provider):
        assert ro_provider.is_readonly()


# ── File tests ──

class TestFile:
    def test_content_length(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        assert res.get_content_length() == 13

    def test_content_type(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        ct = res.get_content_type()
        assert ct == "text/plain"

    def test_display_name(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        assert res.get_display_name() == "hello.txt"

    def test_etag(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        assert res.get_etag() == '"deadbeef"'

    def test_get_content(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        content = res.get_content()
        assert content.read() == b"Hello, world!"

    def test_last_modified(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        dt = res.get_last_modified()
        assert isinstance(dt, datetime)
        assert dt.year == 2026

    def test_creation_date(self, provider):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        dt = res.get_creation_date()
        assert isinstance(dt, datetime)


# ── Directory tests ──

class TestDirectory:
    def test_member_names(self, provider):
        res = provider.get_resource_inst("/", _make_environ(provider))
        names = res.get_member_names()
        assert "hello.txt" in names
        assert "docs" in names

    def test_get_member_file(self, provider):
        root = provider.get_resource_inst("/", _make_environ(provider))
        member = root.get_member("hello.txt")
        assert isinstance(member, PicardFile)

    def test_get_member_dir(self, provider):
        root = provider.get_resource_inst("/", _make_environ(provider))
        member = root.get_member("docs")
        assert isinstance(member, PicardDirectory)

    def test_get_member_missing(self, provider, mock_index):
        root = provider.get_resource_inst("/", _make_environ(provider))
        orig = mock_index.get_manifest_by_path.side_effect

        def patched(p):
            if p == "/nonexistent":
                return None
            return orig(p)

        mock_index.get_manifest_by_path.side_effect = patched
        member = root.get_member("nonexistent")
        assert member is None

    def test_create_empty_resource(self, provider, mock_orch):
        root = provider.get_resource_inst("/", _make_environ(provider))
        child = root.create_empty_resource("new.txt")
        assert isinstance(child, PicardFile)
        mock_orch.put.assert_awaited_once()

    def test_create_collection(self, provider, mock_orch):
        root = provider.get_resource_inst("/", _make_environ(provider))
        child = root.create_collection("newdir")
        assert isinstance(child, PicardDirectory)
        mock_orch.mkdir.assert_awaited_once()

    def test_display_name_root(self, provider):
        root = provider.get_resource_inst("/", _make_environ(provider))
        assert root.get_display_name() == "/"


# ── Read-only tests ──

class TestReadOnly:
    def test_file_begin_write_blocked(self, ro_provider):
        res = ro_provider.get_resource_inst("/hello.txt", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            res.begin_write()

    def test_dir_create_resource_blocked(self, ro_provider):
        root = ro_provider.get_resource_inst("/", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            root.create_empty_resource("blocked.txt")

    def test_dir_create_collection_blocked(self, ro_provider):
        root = ro_provider.get_resource_inst("/", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            root.create_collection("blocked")

    def test_file_delete_blocked(self, ro_provider):
        res = ro_provider.get_resource_inst("/hello.txt", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            res.handle_delete()

    def test_dir_delete_blocked(self, ro_provider):
        res = ro_provider.get_resource_inst("/docs", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            res.handle_delete()

    def test_file_move_blocked(self, ro_provider):
        res = ro_provider.get_resource_inst("/hello.txt", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            res.handle_move("/moved.txt")

    def test_file_copy_blocked(self, ro_provider):
        res = ro_provider.get_resource_inst("/hello.txt", _make_environ(ro_provider))
        from wsgidav.dav_error import DAVError
        with pytest.raises(DAVError):
            res.handle_copy("/copy.txt")


# ── Move/Copy tests (writable) ──

class TestMoveAndCopy:
    def test_file_move(self, provider, mock_orch):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        result = res.handle_move("/renamed.txt")
        assert result is True
        mock_orch.mv.assert_awaited_once_with("/hello.txt", "/renamed.txt")

    def test_file_copy(self, provider, mock_orch):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        result = res.handle_copy("/copy.txt")
        assert result is True
        mock_orch.cp.assert_awaited_once_with("/hello.txt", "/copy.txt")

    def test_file_delete(self, provider, mock_orch):
        res = provider.get_resource_inst("/hello.txt", _make_environ(provider))
        result = res.handle_delete()
        assert result is True
        mock_orch.rm.assert_awaited_once_with("/hello.txt")


# ── Utility tests ──

class TestParseIso:
    def test_valid_iso(self):
        dt = _parse_iso("2026-01-15T10:30:00+00:00")
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 15

    def test_z_suffix(self):
        dt = _parse_iso("2026-06-01T12:00:00Z")
        assert dt.year == 2026

    def test_empty_string(self):
        dt = _parse_iso("")
        assert isinstance(dt, datetime)

    def test_invalid_string(self):
        dt = _parse_iso("not-a-date")
        assert isinstance(dt, datetime)
