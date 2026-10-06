"""Tests for the Google Cloud Functions adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.gcp_functions import GcpFunctionsAdapter


@pytest.fixture
def adapter():
    return GcpFunctionsAdapter(
        function_url="https://us-central1-project.cloudfunctions.net/picard",
        auth_token="test-token",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "gcp-functions"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 10_485_760

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("gcf-test-data")

        assert ref.adapter_name == "gcp-functions"
        assert "gcf://" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"data": "found"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="gcp-functions",
            location="gcf://func/abc123",
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "found"

    async def test_read_missing_raises(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"data": None})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="gcp-functions", location="gcf://func/nope"
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"deleted": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="gcp-functions", location="gcf://func/abc"
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
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

    async def test_unhealthy(self, adapter):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("down"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is False
