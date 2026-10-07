"""Tests for the NEAR Protocol adapter -- mocked, no network calls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.near_adapter import NearAdapter
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


def _make_adapter(**kwargs) -> NearAdapter:
    defaults = dict(
        rpc_url="https://rpc.testnet.near.org",
        account_id="test.testnet",
        private_key="ed25519:testkey",
        contract_id="store.testnet",
    )
    defaults.update(kwargs)
    return NearAdapter(**defaults)


@pytest.mark.asyncio
class TestNearAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "near"
        assert p.max_chunk_bytes == 4_194_304
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_write_chunk(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": {"status": "ok"}})
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "near"
        assert ref.location.startswith("near://")
        a._rpc.assert_awaited_once()
        call_args = a._rpc.call_args
        assert call_args[0][0] == "broadcast_tx_commit"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        payload = json.dumps({"data": enc}).encode()

        a._rpc = AsyncMock(return_value={
            "result": {"result": list(payload)}
        })

        ref = StoredChunkRef(adapter_name="near", location="near://abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": {"result": []}})

        ref = StoredChunkRef(adapter_name="near", location="near://missing")
        with pytest.raises(ValueError, match="no chunk data"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": {"status": "ok"}})

        ref = StoredChunkRef(adapter_name="near", location="near://abc123")
        assert await a.delete_chunk(ref) is True
        a._rpc.assert_awaited_once()
        call_args = a._rpc.call_args
        assert call_args[0][0] == "broadcast_tx_commit"

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._rpc = AsyncMock(side_effect=Exception("tx failed"))

        ref = StoredChunkRef(adapter_name="near", location="near://abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": {"version": {"build": "1.0"}}})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"error": "not synced"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._rpc = AsyncMock(side_effect=Exception("connection refused"))
        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        cap = await a.capacity()
        assert cap["used_bytes"] is None
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
        assert cap["chunk_count"] is None

    async def test_contract_id_defaults_to_account_id(self):
        a = NearAdapter(
            rpc_url="https://rpc.testnet.near.org",
            account_id="myaccount.testnet",
            private_key="ed25519:testkey",
        )
        assert a._contract_id == "myaccount.testnet"
