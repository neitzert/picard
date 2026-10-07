"""Tests for the PICARD MCP server tool handlers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Patch env before importing mcp_server so it picks up test paths
_tmp = tempfile.mkdtemp()
_store = Path(_tmp) / "chunks"
_db = Path(_tmp) / "index.db"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Give each test its own store and database."""
    store = tmp_path / "chunks"
    db = tmp_path / "index.db"
    monkeypatch.setenv("PICARD_PASSPHRASE", "test-passphrase-not-real")
    monkeypatch.setenv("PICARD_STORE", str(store))
    monkeypatch.setenv("PICARD_DB", str(db))

    import picard.mcp_server as mod
    mod._orch = None
    mod._index = None
    mod._DEFAULT_STORE = store
    mod._DEFAULT_DB = db
    yield
    mod._orch = None
    mod._index = None


async def _call(name: str, args: dict | None = None) -> dict | str:
    from picard.mcp_server import call_tool
    result = await call_tool(name, args or {})
    text = result[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class TestPicardMCP:
    async def test_put_and_get_roundtrip(self):
        r = await _call("picard_put", {"path": "/test/hello.txt", "content": "hello world"})
        assert r["status"] == "stored"
        assert r["size"] == 11

        content = await _call("picard_get", {"path": "/test/hello.txt"})
        assert content == "hello world"

    async def test_put_base64_roundtrip(self):
        import base64
        raw = bytes(range(256))
        b64 = base64.b64encode(raw).decode()

        r = await _call("picard_put", {
            "path": "/test/binary.bin",
            "content": b64,
            "encoding": "base64",
        })
        assert r["status"] == "stored"
        assert r["size"] == 256

        content = await _call("picard_get", {
            "path": "/test/binary.bin",
            "encoding": "base64",
        })
        assert base64.b64decode(content) == raw

    async def test_ls_empty(self):
        entries = await _call("picard_ls", {"path": "/"})
        assert entries == []

    async def test_ls_after_put(self):
        await _call("picard_put", {"path": "/docs/a.txt", "content": "aaa"})
        await _call("picard_put", {"path": "/docs/b.txt", "content": "bbb"})

        entries = await _call("picard_ls", {"path": "/docs"})
        names = sorted(e["name"] for e in entries)
        assert names == ["a.txt", "b.txt"]

    async def test_rm(self):
        await _call("picard_put", {"path": "/ephemeral.txt", "content": "gone soon"})
        r = await _call("picard_rm", {"path": "/ephemeral.txt"})
        assert r["status"] == "deleted"

        r2 = await _call("picard_rm", {"path": "/ephemeral.txt"})
        assert r2["status"] == "not_found"

    async def test_mkdir(self):
        r = await _call("picard_mkdir", {"path": "/deep/nested/dir"})
        assert r["status"] == "created"

        stat = await _call("picard_stat", {"path": "/deep/nested/dir"})
        assert stat["type"] == "directory"

    async def test_stat_not_found(self):
        r = await _call("picard_stat", {"path": "/nonexistent"})
        assert r["error"] == "not found"

    async def test_stat_after_put(self):
        await _call("picard_put", {"path": "/meta.txt", "content": "metadata test"})
        r = await _call("picard_stat", {"path": "/meta.txt"})
        assert r["size"] == 13
        assert r["type"] == "regular"
        assert r["version"] == 1

    async def test_tree(self):
        await _call("picard_mkdir", {"path": "/tree-test/sub"})
        await _call("picard_put", {"path": "/tree-test/sub/leaf.txt", "content": "leaf"})

        tree = await _call("picard_tree", {"path": "/tree-test"})
        assert "children" in tree or "name" in tree

    async def test_put_creates_parent_dirs(self):
        await _call("picard_put", {"path": "/auto/parent/file.txt", "content": "nested"})
        content = await _call("picard_get", {"path": "/auto/parent/file.txt"})
        assert content == "nested"

    async def test_no_passphrase_errors(self, monkeypatch):
        monkeypatch.delenv("PICARD_PASSPHRASE", raising=False)
        import picard.mcp_server as mod
        mod._orch = None
        mod._index = None

        r = await _call("picard_put", {"path": "/fail.txt", "content": "nope"})
        assert "PICARD_PASSPHRASE" in r

    async def test_unknown_tool(self):
        r = await _call("picard_bogus", {"path": "/"})
        assert "unknown tool" in r

    async def test_version_increments(self):
        await _call("picard_put", {"path": "/versioned.txt", "content": "v1"})
        r1 = await _call("picard_stat", {"path": "/versioned.txt"})
        assert r1["version"] == 1

        await _call("picard_put", {"path": "/versioned.txt", "content": "v2"})
        r2 = await _call("picard_stat", {"path": "/versioned.txt"})
        assert r2["version"] == 2

    async def test_get_nonexistent_errors(self):
        r = await _call("picard_get", {"path": "/does-not-exist.txt"})
        assert "error" in r.lower() if isinstance(r, str) else "error" in r
