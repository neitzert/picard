"""Tests for the PICARD web dashboard API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from picard.web import app


_MINIMAL_TOML = """\
[general]
index_db = "~/.picard/index.db"
block_size = 65536
"""


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Redirect all config I/O to a temp directory."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_MINIMAL_TOML, encoding="utf-8")
    monkeypatch.setattr("picard.config.DEFAULT_CONFIG_PATH", cfg_path)
    monkeypatch.setattr("picard.web.DEFAULT_CONFIG_PATH", cfg_path)


@pytest.fixture()
def client():
    return TestClient(app)


class TestIndex:
    def test_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "PICARD" in resp.text


class TestAdapterRegistry:
    def test_returns_all_adapters(self, client):
        resp = client.get("/api/adapters/registry")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 250
        first = data[0]
        assert "type" in first
        assert "class" in first
        assert "params" in first
        assert "category" in first

    def test_params_have_required_field(self, client):
        data = client.get("/api/adapters/registry").json()
        with_params = [a for a in data if a["params"]]
        assert len(with_params) > 0
        for p in with_params[0]["params"]:
            assert "name" in p
            assert "required" in p
            assert "is_credential" in p


class TestAdapterConfigure:
    def test_add_and_list(self, client):
        resp = client.post("/api/adapters/configure", json={
            "name": "my-dns",
            "type": "dns",
            "enabled": True,
            "params": {"server": "8.8.8.8"},
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        listed = client.get("/api/adapters/configured").json()
        assert len(listed) == 1
        assert listed[0]["name"] == "my-dns"
        assert listed[0]["type"] == "dns"
        assert listed[0]["enabled"] is True
        assert listed[0]["params"]["server"] == "8.8.8.8"

    def test_update_existing(self, client):
        client.post("/api/adapters/configure", json={
            "name": "a1", "type": "dns", "enabled": True, "params": {},
        })
        client.post("/api/adapters/configure", json={
            "name": "a1", "type": "mqtt", "enabled": False, "params": {"host": "localhost"},
        })
        listed = client.get("/api/adapters/configured").json()
        assert len(listed) == 1
        assert listed[0]["type"] == "mqtt"
        assert listed[0]["enabled"] is False

    def test_toggle(self, client):
        client.post("/api/adapters/configure", json={
            "name": "t1", "type": "dns", "enabled": True, "params": {},
        })
        resp = client.post("/api/adapters/t1/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        resp = client.post("/api/adapters/t1/toggle")
        assert resp.json()["enabled"] is True

    def test_delete(self, client):
        client.post("/api/adapters/configure", json={
            "name": "del-me", "type": "dns", "enabled": True, "params": {},
        })
        assert len(client.get("/api/adapters/configured").json()) == 1
        resp = client.delete("/api/adapters/del-me")
        assert resp.status_code == 200
        assert len(client.get("/api/adapters/configured").json()) == 0


class TestToggle404:
    def test_toggle_nonexistent(self, client):
        resp = client.post("/api/adapters/nonexistent/toggle")
        assert resp.status_code == 404


class TestConfig:
    def test_get_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "block_size" in data
        assert "index_db" in data
        assert "config_path" in data
        assert "config_exists" in data
        assert data["block_size"] == 65536

    def test_update_block_size(self, client):
        resp = client.post("/api/config", json={"block_size": 4096})
        assert resp.status_code == 200

        data = client.get("/api/config").json()
        assert data["block_size"] == 4096

    def test_update_index_db(self, client):
        resp = client.post("/api/config", json={"index_db": "~/.picard/test.db"})
        assert resp.status_code == 200
        data = client.get("/api/config").json()
        assert data["index_db"] == "~/.picard/test.db"

    def test_reject_absolute_index_db(self, client):
        resp = client.post("/api/config", json={"index_db": "/etc/passwd"})
        assert resp.status_code == 400

    def test_reject_traversal_index_db(self, client):
        resp = client.post("/api/config", json={"index_db": "../../etc/passwd"})
        assert resp.status_code == 400

    def test_reject_unknown_config_keys(self, client):
        resp = client.post("/api/config", json={"evil_key": "value"})
        assert resp.status_code == 400


class TestConfigInit:
    def test_init_creates_config(self, client, tmp_path, monkeypatch):
        new_path = tmp_path / "fresh" / "config.toml"
        monkeypatch.setattr("picard.config.DEFAULT_CONFIG_PATH", new_path)
        monkeypatch.setattr("picard.web.DEFAULT_CONFIG_PATH", new_path)
        resp = client.post("/api/config/init")
        assert resp.status_code == 200
        assert new_path.is_file()

    def test_init_conflict(self, client):
        resp = client.post("/api/config/init")
        assert resp.status_code == 409


class TestRaidLevels:
    def test_returns_four_levels(self, client):
        resp = client.get("/api/raid/levels")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 4
        levels = {d["level"] for d in data}
        assert levels == {"raid0", "raid1", "raid5", "raid10"}

    def test_min_adapters(self, client):
        data = client.get("/api/raid/levels").json()
        by_level = {d["level"]: d for d in data}
        assert by_level["raid0"]["min_adapters"] == 2
        assert by_level["raid1"]["min_adapters"] == 2
        assert by_level["raid5"]["min_adapters"] == 3
        assert by_level["raid10"]["min_adapters"] == 4


class TestFiles:
    def test_empty_when_no_db(self, client):
        resp = client.get("/api/files")
        assert resp.status_code == 200
        assert resp.json() == []


class TestStats:
    def test_stats_shape(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_adapters"] >= 250
        assert isinstance(data["categories"], dict)
        assert "network" in data["categories"]
        assert data["configured_adapters"] == 0
        assert data["enabled_adapters"] == 0
        assert "block_size" in data

    def test_stats_after_configure(self, client):
        client.post("/api/adapters/configure", json={
            "name": "x", "type": "dns", "enabled": True, "params": {},
        })
        data = client.get("/api/stats").json()
        assert data["configured_adapters"] == 1
        assert data["enabled_adapters"] == 1


class TestCredentialNoKeyring:
    def test_no_keyring_returns_500(self, client):
        with patch("picard.web.set_credential.__module__", "picard.web"):
            with patch.dict("sys.modules", {"keyring": None}):
                with patch("builtins.__import__", side_effect=_keyring_blocker):
                    resp = client.post("/api/credentials", json={
                        "adapter_name": "test",
                        "param_name": "api_key",
                        "value": "secret",
                    })
                    assert resp.status_code == 500


def _keyring_blocker(name, *args, **kwargs):
    if name == "keyring":
        raise ImportError("no keyring")
    return original_import(name, *args, **kwargs)


import builtins
original_import = builtins.__import__
