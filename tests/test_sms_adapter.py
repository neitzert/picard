"""Tests for SMS/Twilio adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.sms_adapter import SmsAdapter


@pytest.fixture
def adapter():
    return SmsAdapter(
        account_sid="AC_test",
        auth_token="test_token",
        from_number="+15551234567",
        to_number="+15559876543",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "sms"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1600

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"sid": "SM_msg123"})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            with patch("aiohttp.BasicAuth"):
                ref = await adapter.write_chunk("PCHK:m1:0:0:secret")

        assert ref.adapter_name == "sms"
        assert "sms://SM_msg123" == ref.location

    async def test_read_returns_body(self, adapter):
        data = "PCHK:m1:0:0:secret"
        write_resp = AsyncMock()
        write_resp.raise_for_status = MagicMock()
        write_resp.json = AsyncMock(return_value={"sid": "SM_msg456"})

        read_resp = AsyncMock()
        read_resp.raise_for_status = MagicMock()
        read_resp.json = AsyncMock(return_value={"body": data})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=write_resp),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=read_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            with patch("aiohttp.BasicAuth"):
                ref = await adapter.write_chunk(data)
                result = await adapter.read_chunk(ref)

        assert result == data

    async def test_opaque_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"sid": "SM_opaque"})

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            with patch("aiohttp.BasicAuth"):
                ref = await adapter.write_chunk("sealed-envelope-hex-data")

        assert ref.adapter_name == "sms"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete(self, adapter):
        mock_write = AsyncMock()
        mock_write.raise_for_status = MagicMock()
        mock_write.json = AsyncMock(return_value={"sid": "SM_del"})

        mock_del = AsyncMock()
        mock_del.status = 204

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_write),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_session.delete = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_del),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("aiohttp.ClientSession", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_session),
            __aexit__=AsyncMock(return_value=False),
        )):
            with patch("aiohttp.BasicAuth"):
                ref = await adapter.write_chunk("PCHK:m1:0:0:data")
                result = await adapter.delete_chunk(ref)

        assert result is True


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
            with patch("aiohttp.BasicAuth"):
                assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
