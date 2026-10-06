"""Tests for the ix.io adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ix_adapter import IxAdapter


@pytest.fixture
def adapter():
    return IxAdapter()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "ix-io"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 524_288

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="http://ix.io/4abc\n")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("chunk data")

        assert ref.adapter_name == "ix-io"
        assert ref.location == "ix://4abc"


@pytest.mark.asyncio
class TestRead:
    async def test_read_chunk(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="chunk data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="ix-io", location="ix://4abc")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "chunk data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_always_false(self, adapter):
        ref = StoredChunkRef(adapter_name="ix-io", location="ix://4abc")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_empty(self, adapter):
        assert await adapter.list_chunks("any") == []


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True
