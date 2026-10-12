"""Tests for the AWS Lambda adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.lambda_adapter import LambdaAdapter


@pytest.fixture
def adapter():
    return LambdaAdapter(
        function_name="picard-store",
        region="us-east-1",
        aws_access_key_id="AKTEST",
        aws_secret_access_key="secret123",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "aws-lambda"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 6_291_456

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 500


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
            ref = await adapter.write_chunk("lambda-test-data")

        assert ref.adapter_name == "aws-lambda"
        assert "lambda://picard-store/" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"data": "found-it"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="aws-lambda",
            location="lambda://picard-store/picard/abc/b0_c0",
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "found-it"

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
            adapter_name="aws-lambda",
            location="lambda://picard-store/picard/nope",
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
            adapter_name="aws-lambda",
            location="lambda://picard-store/picard/abc",
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
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("timeout"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is False
