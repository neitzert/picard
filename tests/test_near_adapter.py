"""Tests for the NEAR Protocol adapter."""

import json
from unittest.mock import AsyncMock

import pytest

from picard.adapters.near_adapter import NearAdapter


@pytest.fixture
def adapter():
    return NearAdapter(
        rpc_url="https://test.near.org",
        account_id="test.near",
        private_key="ed25519:test",
        contract_id="store.near",
    )


@pytest.mark.asyncio
class TestNearProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "near"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4_194_304

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestNearWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": {"status": "ok"}})
        ref = await adapter.write_chunk("near-chunk-data")
        assert ref.adapter_name == "near"
        assert ref.location.startswith("near://")

    async def test_read_extracts_chunk(self, adapter):
        payload = json.dumps({"data": "recovered-chunk"})
        raw_bytes = list(payload.encode())
        adapter._rpc = AsyncMock(return_value={
            "result": {"result": raw_bytes}
        })
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "near", "location": "near://key123"})()
        )
        assert result == "recovered-chunk"

    async def test_read_empty_raises(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": {"result": []}})
        with pytest.raises(ValueError, match="no chunk data"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "near", "location": "near://missing"})()
            )


@pytest.mark.asyncio
class TestNearOps:
    async def test_delete_success(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": {"status": "ok"}})
        ref = type("R", (), {"adapter_name": "near", "location": "near://key1"})()
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter):
        adapter._rpc = AsyncMock(side_effect=Exception("tx failed"))
        ref = type("R", (), {"adapter_name": "near", "location": "near://key2"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("m1") == []

    async def test_health_check_ok(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": {"chain_id": "mainnet"}})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._rpc = AsyncMock(side_effect=Exception("unreachable"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert all(v is None for v in cap.values())

    async def test_contract_defaults_to_account(self):
        a = NearAdapter(account_id="me.near")
        assert a._contract_id == "me.near"
