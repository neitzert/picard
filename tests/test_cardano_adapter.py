"""Tests for the Cardano adapter."""

from unittest.mock import AsyncMock

import pytest

from picard.adapters.cardano_adapter import CardanoAdapter


@pytest.fixture
def adapter():
    return CardanoAdapter(
        api_url="https://test.blockfrost.io/api/v0",
        project_id="test_project_id",
    )


@pytest.mark.asyncio
class TestCardanoProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "cardano"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 16_384

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


@pytest.mark.asyncio
class TestCardanoWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._post = AsyncMock(return_value={"tx_hash": "abc123"})
        ref = await adapter.write_chunk("cardano-chunk")
        assert ref.adapter_name == "cardano"
        assert ref.location.startswith("ada://")

    async def test_read_extracts_metadata_string(self, adapter):
        adapter._get = AsyncMock(return_value=[
            {"label": "8888", "json_metadata": "hidden-data"}
        ])
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "cardano", "location": "ada://tx1"})()
        )
        assert result == "hidden-data"

    async def test_read_extracts_metadata_dict(self, adapter):
        adapter._get = AsyncMock(return_value=[
            {"label": "8888", "json_metadata": {"chunk": "dict-data"}}
        ])
        result = await adapter.read_chunk(
            type("R", (), {"adapter_name": "cardano", "location": "ada://tx2"})()
        )
        assert result == "dict-data"

    async def test_read_wrong_label_raises(self, adapter):
        adapter._get = AsyncMock(return_value=[
            {"label": "9999", "json_metadata": "wrong"}
        ])
        with pytest.raises(ValueError, match="no metadata label"):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "cardano", "location": "ada://bad"})()
            )

    async def test_read_empty_raises(self, adapter):
        adapter._get = AsyncMock(return_value={"error": "not found"})
        with pytest.raises(ValueError):
            await adapter.read_chunk(
                type("R", (), {"adapter_name": "cardano", "location": "ada://missing"})()
            )


@pytest.mark.asyncio
class TestCardanoOps:
    async def test_delete_returns_false(self, adapter):
        ref = type("R", (), {"adapter_name": "cardano", "location": "ada://tx1"})()
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks_empty(self, adapter):
        assert await adapter.list_chunks("m1") == []

    async def test_health_check_ok(self, adapter):
        adapter._get = AsyncMock(return_value={"is_healthy": True})
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        adapter._get = AsyncMock(side_effect=Exception("down"))
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        cap = await adapter.capacity()
        assert all(v is None for v in cap.values())
