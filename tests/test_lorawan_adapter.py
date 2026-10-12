"""Tests for LoRaWAN adapter."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.lorawan_adapter import LorawanAdapter


@pytest.fixture
def adapter():
    return LorawanAdapter(
        api_url="https://chirpstack.local",
        api_key="test-key",
        application_id="app-1",
        device_eui="0011223344556677",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "lorawan"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 242

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 6


def _mock_session(*methods):
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={})
    session = AsyncMock()
    session.closed = False
    for method in methods:
        setattr(session, method, MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        )))
    return session


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        adapter._session = _mock_session("post")
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        assert ref.adapter_name == "lorawan"
        assert "lora://app-1/0011223344556677" in ref.location

    async def test_read_from_cache(self, adapter):
        adapter._session = _mock_session("post")
        data = "PCHK:m1:0:0:hello"
        ref = await adapter.write_chunk(data)
        result = adapter._written[ref.location]
        assert result == data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        adapter._session = _mock_session("post", "delete")
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        result = await adapter.delete_chunk(ref)
        assert result is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="lorawan", location="lora://fake")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        adapter._session = _mock_session("post")
        await adapter.write_chunk("data-a")
        await adapter.write_chunk("data-b")
        refs = await adapter.list_chunks("app-1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        adapter._session = _mock_session("get")
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        adapter._session = AsyncMock()
        adapter._session.closed = False
        adapter._session.get = MagicMock(side_effect=ConnectionError)
        assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
