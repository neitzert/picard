"""Tests for the Polygon adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.polygon_adapter import PolygonAdapter
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


def _make_adapter(**kwargs) -> PolygonAdapter:
    defaults: dict = dict(
        rpc_url="https://polygon-rpc.com",
        private_key="0x" + "ab" * 32,
    )
    defaults.update(kwargs)
    return PolygonAdapter(**defaults)


@pytest.mark.asyncio
class TestPolygonAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "polygon"
        assert p.max_chunk_bytes == 32_768
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    def test_custom_chain_id(self):
        a = _make_adapter(chain_id=80001)
        assert a._chain_id == 80001

    async def test_write_chunk(self):
        pytest.importorskip("eth_account")
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "0xtxhash"})
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "polygon"
        assert ref.location.startswith("polygon://0x")
        a._rpc.assert_awaited_once()
        args = a._rpc.call_args
        assert args[0][0] == "eth_sendRawTransaction"

    async def test_write_chunk_calldata_hex(self):
        pytest.importorskip("eth_account")
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "0xtxhash"})
        enc = _sample_encoded()

        await a.write_chunk(enc)

        call_params = a._rpc.call_args[0][1]
        calldata = call_params[0]
        assert calldata.startswith("0x")
        decoded = bytes.fromhex(calldata[2:]).decode()
        assert decoded == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        hex_input = "0x" + enc.encode().hex()

        a._rpc = AsyncMock(return_value={
            "result": {"input": hex_input}
        })

        ref = StoredChunkRef(adapter_name="polygon", location="polygon://0xtxhash123")
        result = await a.read_chunk(ref)

        assert result == enc
        a._rpc.assert_awaited_once_with("eth_getTransactionByHash", ["0xtxhash123"])

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": None})

        ref = StoredChunkRef(adapter_name="polygon", location="polygon://0xmissing")
        with pytest.raises(ValueError, match="transaction not found"):
            await a.read_chunk(ref)

    async def test_read_chunk_no_calldata(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": {"input": "0x"}})

        ref = StoredChunkRef(adapter_name="polygon", location="polygon://0xempty")
        with pytest.raises(ValueError, match="no calldata"):
            await a.read_chunk(ref)

    async def test_delete_chunk_always_false(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="polygon", location="polygon://0xtxhash")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._rpc = AsyncMock(return_value={"result": "0x1234"})
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
