"""Tests for the Twitter/X adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.twitter_adapter import TwitterAdapter


@pytest.fixture
def adapter():
    return TwitterAdapter("test-bearer-token")


@pytest.fixture
def dm_adapter():
    return TwitterAdapter(
        "test-bearer-token",
        use_dm=True,
        dm_recipient_id="999",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "twitter"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_tweet_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 280

    def test_dm_max_chunk(self, dm_adapter):
        assert dm_adapter.profile().max_chunk_bytes == 10_000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 15


@pytest.mark.asyncio
class TestWrite:
    async def test_write_tweet(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"data": {"id": "tweet123"}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("short data")

        assert ref.adapter_name == "twitter"
        assert ref.location == "twitter://tweet123"

    async def test_write_dm(self, dm_adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={"data": {"dm_event_id": "dm456"}}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await dm_adapter.write_chunk("longer data for dm")

        assert ref.location == "twitter://dm/dm456"


@pytest.mark.asyncio
class TestRead:
    async def test_read_tweet(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={"data": {"text": "chunk data"}}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="twitter", location="twitter://tweet123")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "chunk data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_tweet(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"data": {"deleted": True}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.delete = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(adapter_name="twitter", location="twitter://tweet123")

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_dm_returns_false(self, adapter):
        ref = StoredChunkRef(adapter_name="twitter", location="twitter://dm/dm456")
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
