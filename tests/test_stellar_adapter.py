"""Tests for the Stellar adapter."""

from unittest.mock import AsyncMock

import pytest

from picard.adapters.stellar_adapter import StellarAdapter


@pytest.fixture
def adapter():
    return StellarAdapter(horizon_url="https://test.stellar.org", secret_key="STEST")


@pytest.mark.asyncio
class TestStellarProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "stellar"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 64

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestStellarWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._post = AsyncMock(return_value={"hash": "txhash"})
        ref = await adapter.write_chunk("tiny-chunk")
        assert ref.adapter_name == "stellar"
        assert ref.location.startswith("xlm://")

    async def test_read_from_memo(self, adapter):
        adapter._get = AsyncMock(return_value={"memo": "memo-data"})
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "stellar", "location": "xlm://tx1"})()
        )
        assert result == "memo-data"

    async def test_read_from_manage_data(self, adapter):
        async def mock_get(path):
            if "effects" in path:
                return {
                    "_embedded": {
                        "records": [
                            {"type": "manage_data", "value": "managed-value"}
                        ]
                    }
                }
            return {"memo": None}

        adapter._get = AsyncMock(side_effect=mock_get)
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "stellar", "location": "xlm://tx2"})()
        )
        assert result == "managed-value"

    async def test_read_missing_raises(self, adapter):
        async def mock_get(path):
            if "effects" in path:
                return {"_embedded": {"records": []}}
            return {}

        adapter._get = AsyncMock(side_effect=mock_get)
        with pytest.raises(ValueError, match="no chunk data"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "stellar", "location": "xlm://nope"})()
            )


@pytest.mark.asyncio
class TestStellarOps:
    async def test_delete_returns_false(self, adapter):
        ref = type("R", (), {"adapter_name": "stellar", "location": "xlm://tx1"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("m1") == []

    async def test_health_check_ok(self, adapter):
        adapter._get = AsyncMock(return_value={"horizon_version": "2.0"})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._get = AsyncMock(side_effect=Exception("down"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert cap["free_bytes"] is None
