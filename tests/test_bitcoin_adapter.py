"""Tests for the Bitcoin OP_RETURN adapter -- mocked, no real node."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.bitcoin_adapter import BitcoinAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"tiny",
    ).encode()


def _make_adapter(**kw) -> BitcoinAdapter:
    defaults = dict(rpc_url="http://127.0.0.1:8332", rpc_user="btc", rpc_password="pass")
    defaults.update(kw)
    return BitcoinAdapter(**defaults)


@pytest.mark.asyncio
class TestBitcoinProfile:
    async def test_defaults(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "bitcoin"
        assert p.max_chunk_bytes == 80
        assert p.persistent is True
        assert p.rate_limit_per_min == 10

    async def test_custom_max(self):
        a = _make_adapter(max_chunk_bytes=40)
        assert a.profile().max_chunk_bytes == 40


@pytest.mark.asyncio
class TestBitcoinWrite:
    async def test_write_chunk(self):
        a = _make_adapter()
        fake_utxo = [{"txid": "abc123", "vout": 0, "amount": 0.001}]

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.side_effect = [
                fake_utxo,                  # listunspent
                "tb1qchangeaddr",           # getrawchangeaddress
                "0200000001...",            # createrawtransaction
                {"complete": True, "hex": "signed_hex"},  # signrawtransactionwithwallet
                "txid_result_hash",         # sendrawtransaction
            ]
            ref = await a.write_chunk("short")

        assert ref.adapter_name == "bitcoin"
        assert ref.location == "btc://txid_result_hash/0"

    async def test_write_no_utxos(self):
        a = _make_adapter()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = []
            with pytest.raises(RuntimeError, match="no UTXOs"):
                await a.write_chunk("data")


@pytest.mark.asyncio
class TestBitcoinRead:
    async def test_read_chunk(self):
        a = _make_adapter()
        data = "hello"
        data_hex = data.encode().hex()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {
                "vout": [
                    {"scriptPubKey": {"asm": f"OP_RETURN {data_hex}"}},
                ]
            }
            ref = StoredChunkRef(adapter_name="bitcoin", location="btc://txid123/0")
            result = await a.read_chunk(ref)

        assert result == data

    async def test_read_no_opreturn(self):
        a = _make_adapter()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {
                "vout": [
                    {"scriptPubKey": {"asm": "OP_DUP OP_HASH160 abc OP_EQUALVERIFY OP_CHECKSIG"}},
                ]
            }
            ref = StoredChunkRef(adapter_name="bitcoin", location="btc://txid123/0")
            with pytest.raises(KeyError):
                await a.read_chunk(ref)


@pytest.mark.asyncio
class TestBitcoinDelete:
    async def test_delete_returns_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="bitcoin", location="btc://txid123/0")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestBitcoinList:
    async def test_list_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []


@pytest.mark.asyncio
class TestBitcoinHealth:
    async def test_health_ok(self):
        a = _make_adapter()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"chain": "main", "blocks": 800000}
            assert await a.health_check() is True

    async def test_health_fail(self):
        a = _make_adapter()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.side_effect = Exception("connection refused")
            assert await a.health_check() is False


@pytest.mark.asyncio
class TestBitcoinCapacity:
    async def test_capacity(self):
        a = _make_adapter()

        with patch.object(a, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"balance": 0.5}
            cap = await a.capacity()

        assert cap["free_bytes"] == 50_000_000
        assert cap["used_bytes"] is None
