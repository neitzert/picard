"""Tests for IcmpInflightAdapter.

All scapy, socket, and platform calls are mocked. No CAP_NET_RAW or Linux
required to run these tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures: inject fake scapy and fake Linux platform before importing adapter
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_platform_and_scapy(monkeypatch):
    """Patch sys.platform to 'linux' and inject a minimal scapy stub."""
    monkeypatch.setattr(sys, "platform", "linux")

    # Build a minimal scapy stub module tree
    scapy_pkg   = types.ModuleType("scapy")
    scapy_all   = types.ModuleType("scapy.all")

    fake_icmp_layer = MagicMock()
    fake_icmp_layer.type = 0
    fake_icmp_layer.id   = 0xCAFE
    fake_icmp_layer.seq  = 0

    class FakeAsyncSniffer:
        def __init__(self, **kwargs):
            self._prn = kwargs.get("prn")
        def start(self):   pass
        def stop(self):    pass

    scapy_all.IP            = MagicMock(return_value=MagicMock())
    scapy_all.ICMP          = MagicMock(return_value=fake_icmp_layer)
    scapy_all.sendp         = MagicMock()
    scapy_all.AsyncSniffer  = FakeAsyncSniffer
    scapy_all.conf          = MagicMock()

    scapy_pkg.all = scapy_all

    monkeypatch.setitem(sys.modules, "scapy",     scapy_pkg)
    monkeypatch.setitem(sys.modules, "scapy.all", scapy_all)

    yield scapy_all


@pytest.fixture()
def adapter(mock_platform_and_scapy):
    from picard.adapters.icmp_inflight_adapter import IcmpInflightAdapter
    a = IcmpInflightAdapter(interface="lo", icmp_id_base=1000)
    yield a
    asyncio.get_event_loop().run_until_complete(a.close())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


SMALL_CHUNK = "hello-world-chunk"
# 1393 bytes -- exceeds single fragment limit of 1392
LARGE_CHUNK = "X" * 1393


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWriteChunk:
    def test_returns_stored_chunk_ref(self, adapter):
        from picard.adapters.base import StoredChunkRef
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        assert isinstance(ref, StoredChunkRef)
        assert ref.adapter_name == "icmp-inflight"

    def test_location_contains_icmp_ids(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        loc = json.loads(ref.location)
        assert "icmp_ids" in loc
        assert len(loc["icmp_ids"]) >= 1

    def test_small_chunk_is_single_fragment(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        loc = json.loads(ref.location)
        assert loc["frag_count"] == 1

    def test_fragment_payload_stored_in_relay(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        loc = json.loads(ref.location)
        icmp_id = loc["icmp_ids"][0]
        payload = adapter._relay.get(icmp_id)
        assert payload is not None


class TestFragmentation:
    def test_large_chunk_splits_into_multiple_fragments(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(LARGE_CHUNK))
        loc = json.loads(ref.location)
        assert loc["frag_count"] == 2
        assert len(loc["icmp_ids"]) == 2

    def test_each_fragment_has_unique_icmp_id(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(LARGE_CHUNK))
        loc = json.loads(ref.location)
        assert len(set(loc["icmp_ids"])) == len(loc["icmp_ids"])


class TestReadChunk:
    def test_read_returns_original_chunk(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        result = run(adapter.read_chunk(ref))
        assert result == SMALL_CHUNK

    def test_read_is_non_destructive(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        run(adapter.read_chunk(ref))
        # Second read must still work
        result2 = run(adapter.read_chunk(ref))
        assert result2 == SMALL_CHUNK

    def test_read_large_chunk_reassembles_correctly(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(LARGE_CHUNK))
        result = run(adapter.read_chunk(ref))
        assert result == LARGE_CHUNK

    def test_read_missing_fragment_raises(self, adapter):
        fake_loc = json.dumps({"icmp_ids": [9999], "frag_count": 1, "data_len": 5})
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="icmp-inflight", location=fake_loc)
        with pytest.raises(KeyError):
            run(adapter.read_chunk(ref))


class TestDeleteChunk:
    def test_delete_returns_true_when_exists(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        found = run(adapter.delete_chunk(ref))
        assert found is True

    def test_delete_removes_from_relay(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        loc = json.loads(ref.location)
        run(adapter.delete_chunk(ref))
        for icmp_id in loc["icmp_ids"]:
            assert adapter._relay.get(icmp_id) is None

    def test_delete_returns_false_when_already_gone(self, adapter):
        with patch.object(adapter, "_inject"):
            ref = run(adapter.write_chunk(SMALL_CHUNK))
        run(adapter.delete_chunk(ref))
        found = run(adapter.delete_chunk(ref))
        assert found is False


class TestHealthCheck:
    def test_returns_true_when_socket_opens(self, adapter):
        import picard.adapters.icmp_inflight_adapter as mod
        mock_sock_instance = MagicMock()
        mock_sock_cls = MagicMock(return_value=mock_sock_instance)
        with patch.object(mod, "socket", MagicMock(
            socket=mock_sock_cls,
            AF_PACKET=3,
            SOCK_RAW=3,
            htons=MagicMock(return_value=0x0800),
        )):
            result = run(adapter.health_check())
        assert result is True

    def test_returns_false_on_permission_error(self, adapter):
        import picard.adapters.icmp_inflight_adapter as mod
        mock_sock_cls = MagicMock(side_effect=PermissionError("no cap_net_raw"))
        with patch.object(mod, "socket", MagicMock(
            socket=mock_sock_cls,
            AF_PACKET=3,
            SOCK_RAW=3,
            htons=MagicMock(return_value=0x0800),
        )):
            result = run(adapter.health_check())
        assert result is False

    def test_returns_false_when_scapy_missing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        # Remove scapy from sys.modules to simulate missing import
        import importlib
        import sys as _sys
        _sys.modules.pop("scapy", None)
        _sys.modules.pop("scapy.all", None)

        # Reload adapter with scapy absent; health_check should still return False
        with patch("picard.adapters.icmp_inflight_adapter._SCAPY_OK", False):
            from picard.adapters.icmp_inflight_adapter import IcmpInflightAdapter
            a = IcmpInflightAdapter.__new__(IcmpInflightAdapter)
            result = asyncio.get_event_loop().run_until_complete(a.health_check())
        assert result is False


class TestPlatformGuard:
    def test_raises_not_implemented_on_non_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        # Need to invalidate cached module so _require_linux() is re-evaluated
        import importlib
        import picard.adapters.icmp_inflight_adapter as mod
        with pytest.raises(NotImplementedError, match="Linux"):
            from picard.adapters.icmp_inflight_adapter import _require_linux
            _require_linux()

    def test_import_error_when_scapy_missing(self, monkeypatch):
        from picard.adapters.icmp_inflight_adapter import _require_scapy
        with patch("picard.adapters.icmp_inflight_adapter._SCAPY_OK", False):
            with pytest.raises(ImportError, match="scapy"):
                _require_scapy()


class TestNonceAllocation:
    def test_multiple_writes_use_distinct_icmp_ids(self, adapter):
        all_ids: list[int] = []
        with patch.object(adapter, "_inject"):
            for _ in range(5):
                ref = run(adapter.write_chunk(SMALL_CHUNK))
                loc = json.loads(ref.location)
                all_ids.extend(loc["icmp_ids"])
        assert len(set(all_ids)) == len(all_ids)
