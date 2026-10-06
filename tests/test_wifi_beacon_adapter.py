"""Tests for the WiFi beacon adapter."""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.wifi_beacon import WifiBeaconAdapter


@pytest.fixture
def adapter():
    return WifiBeaconAdapter(interface="wlan0mon")


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "wifi-beacon"

    async def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 600

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 255


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.wifi_beacon.sendp")
    @patch("picard.adapters.wifi_beacon.Dot11Elt", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11Beacon", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11", MagicMock)
    @patch("picard.adapters.wifi_beacon.RadioTap", MagicMock)
    async def test_round_trip(self, mock_sendp, adapter):
        ref = await adapter.write_chunk("wifi-data")
        assert ref.adapter_name == "wifi-beacon"
        assert "wifi://" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == "wifi-data"

    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="wifi-beacon", location="wifi://missing")
        with pytest.raises((KeyError, Exception)):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.wifi_beacon.sendp")
    @patch("picard.adapters.wifi_beacon.Dot11Elt", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11Beacon", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11", MagicMock)
    @patch("picard.adapters.wifi_beacon.RadioTap", MagicMock)
    async def test_delete(self, mock_sendp, adapter):
        ref = await adapter.write_chunk("del-wifi")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    @patch("picard.adapters.wifi_beacon.sendp")
    @patch("picard.adapters.wifi_beacon.Dot11Elt", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11Beacon", MagicMock)
    @patch("picard.adapters.wifi_beacon.Dot11", MagicMock)
    @patch("picard.adapters.wifi_beacon.RadioTap", MagicMock)
    async def test_list(self, mock_sendp, adapter):
        await adapter.write_chunk("PCHK:1:man1:0:0:1:ZGF0YTE=")
        refs = await adapter.list_chunks("man1")
        assert len(refs) >= 1


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0
        assert cap["free_bytes"] is None


@pytest.mark.asyncio
class TestHealthCheck:
    @patch("picard.adapters.wifi_beacon.subprocess")
    async def test_healthy(self, mock_sub, adapter):
        mock_result = MagicMock()
        mock_result.stdout = "wlan0mon  IEEE 802.11  Mode:Monitor"
        mock_sub.run.return_value = mock_result

        assert await adapter.health_check() is True

    @patch("picard.adapters.wifi_beacon.subprocess")
    async def test_unhealthy(self, mock_sub, adapter):
        mock_sub.run.side_effect = Exception("no interface")
        assert await adapter.health_check() is False
