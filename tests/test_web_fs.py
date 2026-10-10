"""Tests for the PICARD web dashboard filesystem/peers/journal API endpoints."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from picard.index import Index
from picard.manifest import Manifest
from picard.web import _set_state, app


_MINIMAL_TOML = """\
[general]
index_db = ":memory:"
block_size = 65536
"""


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_MINIMAL_TOML, encoding="utf-8")
    monkeypatch.setattr("picard.config.DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr("picard.web.DEFAULT_CONFIG_PATH", cfg_path)


@pytest.fixture()
def index(monkeypatch):
    import sqlite3
    original_connect = sqlite3.connect

    def _connect_no_thread_check(*args, **kwargs):
        kwargs["check_same_thread"] = False
        return original_connect(*args, **kwargs)

    monkeypatch.setattr("sqlite3.connect", _connect_no_thread_check)
    idx = Index(":memory:")
    return idx


@pytest.fixture()
def orch(index):
    mock = AsyncMock()
    mock.index = index
    return mock


@pytest.fixture()
def client(orch, index):
    _set_state(orch, index)
    c = TestClient(app)
    yield c
    _set_state(None, None)


class TestFsLs:
    def test_empty_ls(self, client):
        resp = client.get("/api/fs/ls")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_ls_returns_files(self, client, index):
        m = Manifest(path="/hello.txt", file_type="regular", original_size=42, version=1)
        index.save_manifest(m)
        resp = client.get("/api/fs/ls")
        data = resp.json()
        assert len(data) >= 1
        names = [f["path"] for f in data]
        assert "/hello.txt" in names


class TestFsStat:
    def test_stat_missing(self, client):
        resp = client.get("/api/fs/stat?path=/nope")
        assert resp.status_code == 404

    def test_stat_found(self, client, index):
        m = Manifest(path="/doc.txt", file_type="regular", original_size=100, version=2, mode=0o100644)
        index.save_manifest(m)
        resp = client.get("/api/fs/stat?path=/doc.txt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/doc.txt"
        assert data["type"] == "regular"
        assert data["size"] == 100
        assert data["version"] == 2


class TestFsVersions:
    def test_versions(self, client, index):
        m1 = Manifest(path="/v.txt", file_type="regular", original_size=10, version=1)
        m2 = Manifest(path="/v.txt", file_type="regular", original_size=20, version=2)
        index.save_manifest(m1)
        index.save_manifest(m2)
        resp = client.get("/api/fs/versions?path=/v.txt")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["version"] == 1
        assert data[1]["version"] == 2


class TestFsMkdir:
    def test_mkdir_no_orch_503(self, index):
        _set_state(None, index)
        c = TestClient(app)
        resp = c.post("/api/fs/mkdir", json={"path": "/newdir"})
        assert resp.status_code == 503
        _set_state(None, None)

    def test_mkdir(self, client, orch):
        orch.mkdir.return_value = Manifest(path="/newdir", file_type="directory", manifest_id="abc123")
        resp = client.post("/api/fs/mkdir", json={"path": "/newdir", "parents": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "/newdir"
        orch.mkdir.assert_called_once()


class TestFsRm:
    def test_rm(self, client, orch):
        orch.rm.return_value = True
        resp = client.delete("/api/fs/rm?path=/old.txt")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_rm_nonempty_dir_returns_409(self, client, orch):
        orch.rm.side_effect = OSError("directory not empty: /parent")
        resp = client.delete("/api/fs/rm?path=/parent")
        assert resp.status_code == 409


class TestPeers:
    def test_list_peers_empty(self, client, index):
        resp = client.get("/api/peers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_and_list_peer(self, client, index):
        resp = client.post("/api/peers", json={"url": "http://peer1:8471"})
        assert resp.status_code == 200
        assert resp.json()["added"] == "http://peer1:8471"

        resp = client.get("/api/peers")
        peers = resp.json()
        assert len(peers) == 1
        assert peers[0]["url"] == "http://peer1:8471"

    def test_remove_peer(self, client, index):
        client.post("/api/peers", json={"url": "http://peer2:8471"})
        resp = client.delete("/api/peers?url=http://peer2:8471")
        assert resp.status_code == 200

        resp = client.get("/api/peers")
        assert resp.json() == []


class TestJournal:
    def test_journal_empty(self, client, index):
        resp = client.get("/api/journal")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_recover_no_orch(self, index):
        _set_state(None, index)
        c = TestClient(app)
        resp = c.post("/api/recover")
        assert resp.status_code == 503
        _set_state(None, None)

    def test_recover(self, client, orch):
        orch.recover.return_value = {"cleaned": 0, "rolled_back": 0}
        resp = client.post("/api/recover")
        assert resp.status_code == 200
        assert resp.json()["cleaned"] == 0
