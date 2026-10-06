"""Tests for the Pastebin adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.pastebin_adapter import PastebinAdapter


@pytest.fixture
def adapter():
    return PastebinAdapter("test-api-key", api_user_key="test-user-key")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "pastebin"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 512_000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="https://pastebin.com/AbCdEf12")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("chunk data")

        assert ref.adapter_name == "pastebin"
        assert ref.location == "paste://AbCdEf12"


@pytest.mark.asyncio
class TestRead:
    async def test_read_with_user_key(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="chunk data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="pastebin", location="paste://AbCdEf12")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "chunk data"

    async def test_read_without_user_key(self):
        adapter = PastebinAdapter("test-api-key")

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="chunk data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="pastebin", location="paste://AbCdEf12")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "chunk data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.text = AsyncMock(return_value="Paste Removed")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="pastebin", location="paste://AbCdEf12")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_no_user_key(self):
        adapter = PastebinAdapter("test-api-key")
        ref = StoredChunkRef(adapter_name="pastebin", location="paste://AbCdEf12")
        assert await adapter.delete_chunk(ref) is False


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
