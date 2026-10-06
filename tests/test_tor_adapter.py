"""Tests for the Tor adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tor_adapter import TorAdapter


@pytest.fixture
def adapter():
    return TorAdapter("http://abc123.onion")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "tor"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.put = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch(
                "picard.adapters.tor_adapter.TorAdapter._make_connector",
                return_value=None,
            ),
        ):
            ref = await adapter.write_chunk("PCHK:1:man1:0:0:1:aGVsbG8=")

        assert ref.adapter_name == "tor"
        assert ref.location == "tor://abc123.onion/chunks/man1/0/0"


@pytest.mark.asyncio
class TestRead:
    async def test_read_returns_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="PCHK:man1:0:0:hello")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(
            adapter_name="tor",
            location="tor://abc123.onion/chunks/man1/0/0",
        )

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch(
                "picard.adapters.tor_adapter.TorAdapter._make_connector",
                return_value=None,
            ),
        ):
            result = await adapter.read_chunk(ref)

        assert result == "PCHK:man1:0:0:hello"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 204
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.delete = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        ref = StoredChunkRef(
            adapter_name="tor",
            location="tor://abc123.onion/chunks/key1",
        )

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch(
                "picard.adapters.tor_adapter.TorAdapter._make_connector",
                return_value=None,
            ),
        ):
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_sock = MagicMock()
        mock_sock.connect = MagicMock()
        mock_sock.close = MagicMock()

        with patch("socket.socket", return_value=mock_sock):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        mock_sock = MagicMock()
        mock_sock.connect = MagicMock(side_effect=Exception("refused"))
        mock_sock.close = MagicMock()

        with patch("socket.socket", return_value=mock_sock):
            assert await adapter.health_check() is False
