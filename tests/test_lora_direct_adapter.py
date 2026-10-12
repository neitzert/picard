"""Tests for direct LoRa radio adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.lora_direct import LoRaDirectAdapter


@pytest.fixture
def adapter():
    return LoRaDirectAdapter(
        serial_port="/dev/ttyUSB0",
        frequency=915_000_000,
        spreading_factor=7,
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "lora-direct"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 255


class TestConfiguration:
    def test_configure_commands(self, adapter):
        cmds = adapter._configure_cmd()
        assert any("FREQ" in c for c in cmds)
        assert any("SF" in c for c in cmds)
        assert any("BW" in c for c in cmds)
        assert any("TXPW" in c for c in cmds)


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_write_returns_ref(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test:0:0:rf")
        assert ref.adapter_name == "lora-direct"
        assert "lora://" in ref.location
        assert "915000000" in ref.location

    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_write_caches_data(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:test:0:0:rfdata")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test:0:0:rfdata"

    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_write_opaque_data(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("deadbeef")
        assert ref.adapter_name == "lora-direct"

    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_packet_counter_increments(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref1 = await adapter.write_chunk("PCHK:a:0:0:d1")
        ref2 = await adapter.write_chunk("PCHK:a:0:1:d2")
        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_delete_existing(self, mock_thread, adapter):
        mock_thread.return_value = None
        ref = await adapter.write_chunk("PCHK:del:0:0:x")
        assert await adapter.delete_chunk(ref) is True

    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_delete_nonexistent(self, mock_thread, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="lora-direct", location="lora://915000000/7/999")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_success(self, adapter):
        with patch("picard.adapters.lora_direct.asyncio.to_thread", return_value=True):
            assert await adapter.health_check() is True

    async def test_health_failure(self, adapter):
        with patch("picard.adapters.lora_direct.asyncio.to_thread", side_effect=Exception):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_capacity_empty(self, mock_thread, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    @patch("picard.adapters.lora_direct.asyncio.to_thread")
    async def test_capacity_after_write(self, mock_thread, adapter):
        mock_thread.return_value = None
        await adapter.write_chunk("PCHK:cap:0:0:rf")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
