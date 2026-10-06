"""Tests for the OpenVPN adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.openvpn_adapter import OpenVpnAdapter


@pytest.fixture
def adapter():
    return OpenVpnAdapter(
        storage_host="http://10.8.0.1:8080",
        management_host="127.0.0.1",
        management_port=7505,
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "openvpn"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 500


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_chunk(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.put = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("ovpn-data")

        assert ref.adapter_name == "openvpn"
        assert ref.location.startswith("ovpn://")

    async def test_read_chunk(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="ovpn-data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="openvpn",
            location="ovpn://http://10.8.0.1:8080/test_key",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "ovpn-data"


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

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="openvpn", location="ovpn://x/k")
        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            from picard.adapters.base import StoredChunkRef

            ref = StoredChunkRef(adapter_name="openvpn", location="ovpn://x/k")
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_connected(self, adapter):
        mock_reader = AsyncMock()
        mock_reader.readline = AsyncMock(
            side_effect=[
                b">INFO:OpenVPN Management Interface\r\n",
                b"1696000000,CONNECTED,SUCCESS,10.8.0.1\r\n",
                b"END\r\n",
            ]
        )
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection", return_value=(mock_reader, mock_writer)
        ):
            assert await adapter.health_check() is True

    async def test_disconnected(self, adapter):
        with patch("asyncio.open_connection", side_effect=ConnectionRefusedError):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_from_server(self, adapter):
        cap_data = {"used_bytes": 500, "free_bytes": 500, "total_bytes": 1000, "chunk_count": 2}
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=cap_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            cap = await adapter.capacity()
        assert cap["chunk_count"] == 2
