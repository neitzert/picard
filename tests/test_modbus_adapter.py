"""Tests for Modbus PLC/RTU adapter."""

import struct
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

mock_pymodbus = MagicMock()
mock_client_mod = MagicMock()
sys.modules.setdefault("pymodbus", mock_pymodbus)
sys.modules.setdefault("pymodbus.client", mock_client_mod)

from picard.adapters.modbus_adapter import ModbusAdapter


@pytest.fixture
def mock_modbus_client():
    client = AsyncMock()
    client.connect = AsyncMock()
    client.close = MagicMock()
    client.write_registers = AsyncMock()
    mock_result = MagicMock()
    mock_result.isError.return_value = False
    mock_result.registers = []
    client.read_holding_registers = AsyncMock(return_value=mock_result)
    mock_client_mod.AsyncModbusTcpClient.return_value = client
    return client


@pytest.fixture
def adapter():
    return ModbusAdapter("192.168.1.100", port=502, unit_id=1, start_register=1000)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "modbus"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 250

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_round_trip(self, adapter, mock_modbus_client):
        data = "test-data-hello"

        ref = await adapter.write_chunk(data)

        assert ref.adapter_name == "modbus"
        assert "modbus://" in ref.location
        assert "192.168.1.100" in ref.location

        raw = data.encode("utf-8")
        n_regs = 2 + (len(raw) + 1) // 2
        length_regs = list(struct.unpack(">HH", struct.pack(">I", len(raw))))
        data_regs = []
        padded = raw + b"\x00" * (len(raw) % 2)
        for i in range(0, len(padded), 2):
            data_regs.append(struct.unpack(">H", padded[i : i + 2])[0])
        all_regs = length_regs + data_regs

        mock_result = MagicMock()
        mock_result.registers = all_regs
        mock_modbus_client.read_holding_registers = AsyncMock(return_value=mock_result)

        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_location_format(self, adapter, mock_modbus_client):
        ref = await adapter.write_chunk("some-data")
        assert ref.location.startswith("modbus://192.168.1.100:502/1/hr/")


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter, mock_modbus_client):
        ref = await adapter.write_chunk("data")
        result = await adapter.delete_chunk(ref)
        assert result is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="modbus", location="modbus://fake/0/hr/0-0")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter, mock_modbus_client):
        await adapter.write_chunk("data-a")
        await adapter.write_chunk("data-b")

        refs = await adapter.list_chunks("")
        assert len(refs) >= 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter, mock_modbus_client):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_client_mod.AsyncModbusTcpClient.return_value.connect = AsyncMock(
            side_effect=ConnectionRefusedError
        )
        assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_after_write(self, adapter, mock_modbus_client):
        await adapter.write_chunk("data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
