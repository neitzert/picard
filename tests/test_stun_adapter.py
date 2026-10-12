"""Tests for the STUN/TURN adapter -- mocked, no real server."""

from __future__ import annotations

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.stun_adapter import StunAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> StunAdapter:
    return StunAdapter(server="turn.example.com", **kwargs)


@pytest.mark.asyncio
class TestStunAdapter:
    async def test_profile(self):
        p = _make_adapter().profile()
        assert p.name == "stun"
        assert p.max_chunk_bytes == 1200
        assert p.persistent is False
        assert p.rate_limit_per_min == 120

    async def test_profile_custom_max(self):
        assert _make_adapter(max_chunk_bytes=800).profile().max_chunk_bytes == 800

    async def test_write_read_round_trip(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = a._location_key(enc)
        a._cache[loc] = enc
        ref = StoredChunkRef(adapter_name="stun", location=loc)
        assert await a.read_chunk(ref) == enc

    async def test_write_opaque_data(self):
        a = _make_adapter()
        loc = a._location_key("opaque-sealed")
        a._cache[loc] = "opaque-sealed"
        ref = StoredChunkRef(adapter_name="stun", location=loc)
        assert await a.read_chunk(ref) == "opaque-sealed"

    async def test_read_missing(self):
        ref = StoredChunkRef(adapter_name="stun", location="turn://x/missing")
        with pytest.raises(KeyError):
            await _make_adapter().read_chunk(ref)

    async def test_delete(self):
        a = _make_adapter()
        enc = _sample_encoded()
        loc = a._location_key(enc)
        a._cache[loc] = enc
        ref = StoredChunkRef(adapter_name="stun", location=loc)
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

    async def test_list_chunks_empty(self):
        assert await _make_adapter().list_chunks("m1") == []

    async def test_capacity(self):
        a = _make_adapter()
        a._cache["k"] = _sample_encoded()
        cap = await a.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0

    async def test_capacity_empty(self):
        cap = await _make_adapter().capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_build_stun_binding(self):
        a = _make_adapter()
        txn_id = b"\x01" * 12
        pkt = a._build_stun_binding(txn_id)
        assert len(pkt) == 20
        assert pkt[4:8] == b"\x21\x12\xa4\x42"

    async def test_build_channel_data(self):
        a = _make_adapter()
        msg = a._build_channel_data(0x4000, b"hello")
        assert len(msg) >= 4 + 5
        assert msg[0:2] == b"\x40\x00"

    async def test_location_structured(self):
        a = _make_adapter()
        loc = a._location_key(_sample_encoded("abc", 1, 2))
        assert "abc" in loc and "1_2" in loc
