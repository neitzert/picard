"""Tests for the Tezos adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tezos_adapter import TezosAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> TezosAdapter:
    defaults = dict(
        rpc_url="https://mainnet.api.tez.ie",
        secret_key="edsk_test_secret_key",
    )
    defaults.update(kwargs)
    return TezosAdapter(**defaults)


@pytest.mark.asyncio
class TestTezosAdapter:
    async def test_profile_name(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "tezos"

    async def test_profile_limits(self):
        a = _make_adapter()
        p = a.profile()
        assert p.max_chunk_bytes == 16_384
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 15

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        with patch("picard.adapters.tezos_adapter._tezos_seed", return_value=b"\x00" * 32), \
             patch("picard.adapters.tezos_adapter._tz1_from_seed", return_value="tz1test"):
            a._rpc_get = AsyncMock(side_effect=["1234", "deadbeef" * 8])
            a._rpc_post = AsyncMock(side_effect=["00" * 64, "ophash123"])
            ref = await a.write_chunk(enc)
        assert ref.adapter_name == "tezos"
        assert ref.location == "xtz://ophash123"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._tzkt_get = AsyncMock(return_value=[
            {"originatedContract": {"address": "KT1test"}}
        ])
        a._rpc_get = AsyncMock(return_value={"string": enc})

        ref = StoredChunkRef(adapter_name="tezos", location="xtz://ophash123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._tzkt_get = AsyncMock(return_value=[])

        ref = StoredChunkRef(adapter_name="tezos", location="xtz://missing")
        with pytest.raises(ValueError, match="not found in TzKT"):
            await a.read_chunk(ref)

    async def test_read_chunk_no_contract(self):
        a = _make_adapter()
        a._tzkt_get = AsyncMock(return_value=[{"originatedContract": {}}])

        ref = StoredChunkRef(adapter_name="tezos", location="xtz://ophash456")
        with pytest.raises(ValueError, match="no originated contract"):
            await a.read_chunk(ref)

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="tezos", location="xtz://ophash123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._rpc_get = AsyncMock(return_value={"level": 12345})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._rpc_get = AsyncMock(return_value={"error": "not synced"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._rpc_get = AsyncMock(side_effect=Exception("connection refused"))
        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap == {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def test_custom_rpc_url(self):
        a = _make_adapter(rpc_url="https://rpc.tzbeta.net/")
        assert a._rpc_url == "https://rpc.tzbeta.net"

    async def test_custom_max_chunk(self):
        a = _make_adapter(max_chunk_bytes=8192)
        p = a.profile()
        assert p.max_chunk_bytes == 8192
