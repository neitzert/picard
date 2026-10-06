"""Tests for the Hacker News adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.hackernews import HackerNewsAdapter


@pytest.fixture
def adapter():
    return HackerNewsAdapter("testuser", "testpass", 12345)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "hackernews"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 4096

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 4


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        # Login response
        mock_login_resp = AsyncMock()
        mock_login_resp.cookies = {"user": MagicMock(value="cookie123")}
        mock_login_resp.__aenter__ = AsyncMock(return_value=mock_login_resp)
        mock_login_resp.__aexit__ = AsyncMock(return_value=False)

        # Comment POST response
        mock_comment_resp = AsyncMock()
        mock_comment_resp.raise_for_status = MagicMock()
        mock_comment_resp.__aenter__ = AsyncMock(return_value=mock_comment_resp)
        mock_comment_resp.__aexit__ = AsyncMock(return_value=False)

        # Parent item GET response
        mock_parent_resp = AsyncMock()
        mock_parent_resp.json = AsyncMock(return_value={"kids": [67890]})
        mock_parent_resp.__aenter__ = AsyncMock(return_value=mock_parent_resp)
        mock_parent_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        call_count = 0

        def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_login_resp
            return mock_comment_resp

        mock_session.post = MagicMock(side_effect=mock_post)
        mock_session.get = MagicMock(return_value=mock_parent_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("test chunk data")

        assert ref.adapter_name == "hackernews"
        assert ref.location == "hn://12345/67890"


@pytest.mark.asyncio
class TestRead:
    async def test_read_strips_marker(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(
            return_value={"text": "[PCHK]test chunk data"}
        )
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(
            adapter_name="hackernews",
            location="hn://12345/67890",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "test chunk data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_returns_false(self, adapter):
        ref = StoredChunkRef(
            adapter_name="hackernews",
            location="hn://12345/67890",
        )
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

    async def test_health_fail(self, adapter):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("fail"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is False
