"""Tests for Zigbee wireless adapter."""

import sys
from unittest.mock import MagicMock, patch

import pytest

mock_zigpy_app = MagicMock()
mock_zigpy_config = MagicMock()
mock_zigpy_config.CONF_DEVICE = "device"
mock_zigpy_config.CONF_DEVICE_PATH = "path"
mock_zigpy_config.CONF_NWK = "nwk"
mock_zigpy_config.CONF_NWK_PAN_ID = "pan_id"
mock_zigpy_config.CONF_NWK_CHANNEL = "channel"

mock_serial = MagicMock()
mock_serial_inst = MagicMock()
mock_serial_inst.close = MagicMock()
mock_serial.Serial.return_value = mock_serial_inst

sys.modules.setdefault("zigpy", MagicMock())
sys.modules.setdefault("zigpy.application", mock_zigpy_app)
sys.modules.setdefault("zigpy.config", mock_zigpy_config)
sys.modules.setdefault("zigpy.zcl", MagicMock())
sys.modules.setdefault("zigpy.zcl.foundation", MagicMock())
sys.modules.setdefault("serial", mock_serial)

from picard.adapters.zigbee_adapter import ZigbeeAdapter


@pytest.fixture
def adapter():
    return ZigbeeAdapter("/dev/ttyUSB0", pan_id=0x1234, channel=15)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "zigbee"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 128

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        assert ref.adapter_name == "zigbee"
        assert "zigbee://0x1234" in ref.location
        assert "0xfc00" in ref.location

    async def test_location_contains_pan_and_cluster(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        assert "0x1234" in ref.location
        assert "0xffff" in ref.location

    async def test_sequential_attributes(self, adapter):
        ref1 = await adapter.write_chunk("PCHK:m1:0:0:a")
        ref2 = await adapter.write_chunk("PCHK:m1:0:1:b")
        assert ref1.location != ref2.location
        assert "0x0000" in ref1.location
        assert "0x0001" in ref2.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="zigbee", location="zigbee://fake")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_filters(self, adapter):
        await adapter.write_chunk("data1")
        await adapter.write_chunk("data2")
        refs = await adapter.list_chunks("")
        assert len(refs) >= 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_serial.Serial.side_effect = OSError
        assert await adapter.health_check() is False
        mock_serial.Serial.side_effect = None


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self):
        a = ZigbeeAdapter("/dev/ttyUSB0")
        cap = await a.capacity()
        assert cap["chunk_count"] == 0

    async def test_after_write(self, adapter):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
