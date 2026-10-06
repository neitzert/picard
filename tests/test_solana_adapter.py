"""Tests for the Solana adapter."""

import base64
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.solana_adapter import SolanaAdapter


@pytest.fixture
def adapter():
    return SolanaAdapter(rpc_url="https://test.solana.com", keypair_path="/tmp/key.json")


@pytest.mark.asyncio
class TestSolanaProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "solana"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 566

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestSolanaWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": "txhash123"})
        ref = await adapter.write_chunk("test-chunk-data")
        assert ref.adapter_name == "solana"
        assert ref.location.startswith("sol://")

    async def test_read_extracts_memo(self, adapter):
        chunk = "hello-picard"
        encoded = base64.b64encode(chunk.encode()).decode()
        adapter._rpc = AsyncMock(return_value={
            "result": {
                "transaction": {
                    "message": {
                        "instructions": [
                            {
                                "programId": SolanaAdapter.MEMO_PROGRAM_ID,
                                "data": encoded,
                            }
                        ]
                    }
                }
            }
        })
        result = await adapter.read_chunk(
            type("Ref", (), {"adapter_name": "solana", "location": "sol://abc123"})()
        )
        assert result == chunk

    async def test_read_missing_tx_raises(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": None})
        with pytest.raises(ValueError, match="not found"):
            await adapter.read_chunk(
                type("Ref", (), {"adapter_name": "solana", "location": "sol://missing"})()
            )

    async def test_read_no_memo_raises(self, adapter):
        adapter._rpc = AsyncMock(return_value={
            "result": {
                "transaction": {"message": {"instructions": []}}
            }
        })
        with pytest.raises(ValueError, match="no memo"):
            await adapter.read_chunk(
                type("Ref", (), {"adapter_name": "solana", "location": "sol://nope"})()
            )


@pytest.mark.asyncio
class TestSolanaOps:
    async def test_delete_returns_false(self, adapter):
        ref = type("Ref", (), {"adapter_name": "solana", "location": "sol://tx1"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("manifest1") == []

    async def test_health_check_ok(self, adapter):
        adapter._rpc = AsyncMock(return_value={"result": "ok"})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._rpc = AsyncMock(side_effect=Exception("connection refused"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] is None
        assert cap["chunk_count"] is None
