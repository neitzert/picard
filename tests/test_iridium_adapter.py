"""Tests for Iridium SBD satellite adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.iridium_adapter import IridiumAdapter


@pytest.fixture
def adapter():
    return IridiumAdapter(
        imei="300234010000000",
        username="testuser",
        password="testpass",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "iridium"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 2

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 340


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"message_id": "sat123"})
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("PCHK:test:0:0:sat")
        assert ref.adapter_name == "iridium"
        assert "iridium://" in ref.location
        assert "sat123" in ref.location

    async def test_write_caches_for_read(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"message_id": "msg1"})
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("PCHK:test:0:0:data")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:data"

    async def test_write_opaque_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"message_id": "msg2"})
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("deadbeef")
        assert ref.adapter_name == "iridium"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        adapter._cache["iridium://300234010000000/msg1"] = "data"
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="iridium", location="iridium://300234010000000/msg1")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="iridium", location="iridium://000/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
