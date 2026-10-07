"""Tests for peer registry."""

import pytest
import sqlite3

from picard.peer import PeerRegistry


@pytest.fixture
def registry():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return PeerRegistry(conn)


class TestPeerRegistry:
    def test_add_and_list(self, registry):
        registry.add("http://peer1:8471", ["memory", "fs"])
        peers = registry.list_peers()
        assert len(peers) == 1
        assert peers[0].url == "http://peer1:8471"
        assert peers[0].adapters == ["memory", "fs"]
        assert peers[0].status == "active"

    def test_remove(self, registry):
        registry.add("http://peer1:8471")
        assert registry.remove("http://peer1:8471")
        assert registry.list_peers() == []

    def test_remove_nonexistent(self, registry):
        assert not registry.remove("http://nope:8471")

    def test_get(self, registry):
        registry.add("http://peer1:8471", ["memory"])
        peer = registry.get("http://peer1:8471")
        assert peer is not None
        assert peer.adapters == ["memory"]

    def test_get_nonexistent(self, registry):
        assert registry.get("http://nope:8471") is None

    def test_update_status(self, registry):
        registry.add("http://peer1:8471")
        registry.update_status("http://peer1:8471", "unreachable")
        peer = registry.get("http://peer1:8471")
        assert peer.status == "unreachable"

    def test_multiple_peers(self, registry):
        registry.add("http://peer1:8471")
        registry.add("http://peer2:8471")
        registry.add("http://peer3:8471")
        peers = registry.list_peers()
        assert len(peers) == 3

    def test_upsert(self, registry):
        registry.add("http://peer1:8471", ["old"])
        registry.add("http://peer1:8471", ["new"])
        peers = registry.list_peers()
        assert len(peers) == 1
        assert peers[0].adapters == ["new"]
