"""Tests for the Ethereum adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ethereum import EthereumAdapter
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


def _make_adapter(**kwargs) -> EthereumAdapter:
    defaults = dict(
        rpc_url="http://localhost:8545",
        contract_address="0x" + "ab" * 20,
        private_key="0x" + "cd" * 32,
    )
    defaults.update(kwargs)
    return EthereumAdapter(**defaults)


class TestEthereumAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ethereum"
        assert p.max_chunk_bytes == 24_000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 15

    def test_key_hash(self):
        a = _make_adapter()
        key = a._key_hash("test123")
        assert key.startswith("0x")
        assert len(key) == 66  # 0x + 64 hex chars

    def test_key_hash_deterministic(self):
        a = _make_adapter()
        assert a._key_hash("abc") == a._key_hash("abc")
        assert a._key_hash("abc") != a._key_hash("def")

    @pytest.mark.asyncio
    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_tx = AsyncMock(return_value="0xtxhash")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ethereum"
        assert len(ref.location) == 16
        a._send_tx.assert_awaited_once()
        call_args = a._send_tx.call_args
        assert call_args[0][0] == EthereumAdapter.STORE_SELECTOR

    @pytest.mark.asyncio
    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        hex_data = "0x" + enc.encode().hex()
        a._eth_call = AsyncMock(return_value=hex_data)

        ref = StoredChunkRef(adapter_name="ethereum", location="abc123def456")
        result = await a.read_chunk(ref)

        assert result == enc
        a._eth_call.assert_awaited_once()
        call_args = a._eth_call.call_args
        assert call_args[0][0] == EthereumAdapter.RETRIEVE_SELECTOR

    @pytest.mark.asyncio
    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._eth_call = AsyncMock(return_value="0x")

        ref = StoredChunkRef(adapter_name="ethereum", location="missing")
        with pytest.raises(ValueError, match="no data"):
            await a.read_chunk(ref)

    @pytest.mark.asyncio
    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_tx = AsyncMock(return_value="0xtxhash")

        ref = StoredChunkRef(adapter_name="ethereum", location="abc123def456")
        assert await a.delete_chunk(ref) is True
        a._send_tx.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_tx = AsyncMock(side_effect=Exception("gas too low"))

        ref = StoredChunkRef(adapter_name="ethereum", location="abc123def456")
        assert await a.delete_chunk(ref) is False

    @pytest.mark.asyncio
    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "0x1234"})
        assert await a.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_fail(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"error": "not synced"})
        assert await a.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_exception(self):
        a = _make_adapter()
        a._rpc = AsyncMock(side_effect=Exception("connection refused"))
        assert await a.health_check() is False

    def test_custom_chain_id(self):
        a = _make_adapter(chain_id=137)
        assert a._chain_id == 137
