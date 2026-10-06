"""Tests for the Polygon adapter."""

from unittest.mock import AsyncMock

import pytest

from picard.adapters.polygon_adapter import PolygonAdapter


@pytest.fixture
def adapter():
    return PolygonAdapter(rpc_url="https://test-polygon.rpc", private_key="0xdeadbeef")


@pytest.mark.asyncio
class TestPolygonProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "polygon"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 32_768

    async def test_chain_id(self, adapter):
        assert adapter._chain_id == 137


@pytest.mark.asyncio
class TestPolygonWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": "0xhash"})
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "polygon"
        assert ref.location.startswith("polygon://")

    async def test_read_extracts_calldata(self, adapter):
        chunk = "hidden-payload"
        hex_data = "0x" + chunk.encode().hex()
        adapter._rpc = AsyncMock(return_value={
            "result": {"input": hex_data}
        })
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "polygon", "location": "polygon://0xabc"})()
        )
        assert result == chunk

    async def test_read_missing_tx_raises(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": None})
        with pytest.raises(ValueError, match="not found"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "polygon", "location": "polygon://0xmissing"})()
            )

    async def test_read_empty_calldata_raises(self, adapter):
        adapter._rpc = AsyncMock(return_value={
            "result": {"input": "0x"}
        })
        with pytest.raises(ValueError, match="no calldata"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "polygon", "location": "polygon://0xempty"})()
            )


@pytest.mark.asyncio
class TestPolygonOps:
    async def test_delete_returns_false(self, adapter):
        ref = type("R", (), {"adapter_name": "polygon", "location": "polygon://0x1"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("m1") == []

    async def test_health_check_ok(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": "0x123"})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._rpc = AsyncMock(side_effect=Exception("timeout"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert all(v is None for v in cap.values())
