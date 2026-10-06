"""Tests for the Tezos adapter."""

from unittest.mock import AsyncMock

import pytest

from picard.adapters.tezos_adapter import TezosAdapter


@pytest.fixture
def adapter():
    return TezosAdapter(rpc_url="https://test.tez.ie", secret_key="edsk_test")


@pytest.mark.asyncio
class TestTezosProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "tezos"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 16_384

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 15


@pytest.mark.asyncio
class TestTezosWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._post = AsyncMock(return_value={"hash": "oabc123"})
        ref = await adapter.write_chunk("tezos-chunk")
        assert ref.adapter_name == "tezos"
        assert ref.location.startswith("xtz://")

    async def test_read_dict_result(self, adapter):
        adapter._get = AsyncMock(return_value={
            "metadata": {"picard_chunk": "secret-data"}
        })
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "tezos", "location": "xtz://op1"})()
        )
        assert result == "secret-data"

    async def test_read_list_result(self, adapter):
        adapter._get = AsyncMock(return_value=[
            {"metadata": {"picard_chunk": "from-list"}}
        ])
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "tezos", "location": "xtz://op2"})()
        )
        assert result == "from-list"

    async def test_read_missing_raises(self, adapter):
        adapter._get = AsyncMock(return_value={"metadata": {}})
        with pytest.raises(ValueError, match="no chunk data"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "tezos", "location": "xtz://missing"})()
            )


@pytest.mark.asyncio
class TestTezosOps:
    async def test_delete_returns_false(self, adapter):
        ref = type("R", (), {"adapter_name": "tezos", "location": "xtz://op1"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("m1") == []

    async def test_health_check_ok(self, adapter):
        adapter._get = AsyncMock(return_value={"level": 1000})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._get = AsyncMock(side_effect=Exception("unreachable"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert cap["total_bytes"] is None
