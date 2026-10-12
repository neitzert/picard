"""Tests for the WireGuard adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.wireguard_adapter import WireguardAdapter


@pytest.fixture
def adapter():
    return WireguardAdapter(
        storage_host="http://10.13.37.1:8080",
        endpoint="10.0.0.1:51820",
        interface="wg0",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "wireguard"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536


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
            ref = await adapter.write_chunk("wg-data")

        assert ref.adapter_name == "wireguard"
        assert ref.location.startswith("wg://")

    async def test_read_chunk(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="wg-data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="wireguard", location="wg://10.0.0.1:51820/test_key"
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "wg-data"


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

        ref = StoredChunkRef(adapter_name="wireguard", location="wg://x/k")
        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"latest handshake: 5 seconds ago", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            assert await adapter.health_check() is True

    async def test_unhealthy_no_handshake(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"interface: wg0", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            assert await adapter.health_check() is False

    async def test_unhealthy_error(self, adapter):
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_from_server(self, adapter):
        cap_data = {"used_bytes": 1000, "free_bytes": 9000, "total_bytes": 10000, "chunk_count": 5}
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
        assert cap["used_bytes"] == 1000

    async def test_fallback_on_error(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception):
            cap = await adapter.capacity()
        assert cap["used_bytes"] is None
