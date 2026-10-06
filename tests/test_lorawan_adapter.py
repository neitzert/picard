"""Tests for LoRaWAN adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"id": "queue-1"})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            ref = await adapter.write_chunk("PCHK:m1:0:0:data")

        assert ref.adapter_name == "lorawan"
        assert "lora://app-1/0011223344556677" in ref.location

    async def test_read_from_cache(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        data = "PCHK:m1:0:0:hello"
        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            ref = await adapter.write_chunk(data)

        result = adapter._written[ref.location]
        assert result == data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session.delete = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
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
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            await adapter.write_chunk("data-a")
            await adapter.write_chunk("data-b")

        # All locations contain the app_id and dev_eui
        refs = await adapter.list_chunks("app-1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=ConnectionError):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
