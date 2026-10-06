"""Tests for the ARP adapter -- mocked, no real network."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.chunker import Chunk

# Stub out scapy before importing the adapter
_scapy_stub = ModuleType("scapy")
_scapy_all_stub = ModuleType("scapy.all")
sys.modules.setdefault("scapy", _scapy_stub)
sys.modules.setdefault("scapy.all", _scapy_all_stub)

from picard.adapters.arp_adapter import ArpAdapter  # noqa: E402


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> ArpAdapter:
    return ArpAdapter(interface="eth0", **kwargs)


async def _run_sync(fn, *a, **kw):
    return fn(*a, **kw)


@pytest.mark.asyncio
class TestArpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "arp"
        assert p.max_chunk_bytes == 128
        assert p.persistent is False
        assert p.rate_limit_per_min == 30

    async def test_profile_custom_max(self):
        assert _make_adapter(max_chunk_bytes=64).profile().max_chunk_bytes == 64

    async def test_write_read_round_trip(self):
        a = _make_adapter()
        enc = _sample_encoded()
        # Bypass scapy send — just populate cache directly
        loc = a._location_key(enc)
        a._cache[loc] = enc
        ref = StoredChunkRef(adapter_name="arp", location=loc)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_write_opaque_data(self):
        a = _make_adapter()
        loc = a._location_key("opaque-no-pchk")
        a._cache[loc] = "opaque-no-pchk"
        ref = StoredChunkRef(adapter_name="arp", location=loc)
        assert await a.read_chunk(ref) == "opaque-no-pchk"

    async def test_read_missing(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="arp", location="arp://eth0/missing")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)

    async def test_delete(self):
        a = _make_adapter()
        loc = a._location_key(_sample_encoded())
        a._cache[loc] = _sample_encoded()
        ref = StoredChunkRef(adapter_name="arp", location=loc)
        assert await a.delete_chunk(ref) is True
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        for i in range(2):
            enc = _sample_encoded("m1", 0, i)
            a._cache[a._location_key(enc)] = enc
        enc2 = _sample_encoded("m2", 0, 0)
        a._cache[a._location_key(enc2)] = enc2
        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "arp" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        assert await a.list_chunks("m1") == []

    async def test_health_check(self):
        assert await _make_adapter().health_check() is True

    async def test_capacity(self):
        a = _make_adapter()
        a._cache["k"] = _sample_encoded()
        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
        assert cap["free_bytes"] is None

    async def test_capacity_empty(self):
        cap = await _make_adapter().capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_location_key_structured(self):
        a = _make_adapter()
        loc = a._location_key(_sample_encoded("abc", 2, 3))
        assert "abc" in loc and "2_3" in loc

    async def test_location_key_opaque(self):
        a = _make_adapter()
        loc = a._location_key("not-a-chunk")
        assert loc.startswith("arp://eth0/")
