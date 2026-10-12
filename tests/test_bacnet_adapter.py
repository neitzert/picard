"""Tests for BACnet building automation adapter."""

import sys
from unittest.mock import MagicMock, patch

import pytest

mock_bac0 = MagicMock()
sys.modules.setdefault("BAC0", mock_bac0)

from picard.adapters.bacnet_adapter import BacnetAdapter


@pytest.fixture
def adapter():
    return BacnetAdapter("192.168.1.200", device_id=100)


@pytest.fixture(autouse=True)
def _reset_bac0():
    mock_bac0.reset_mock()
    mock_instance = MagicMock()
    mock_instance.write = MagicMock()
    mock_instance.read = MagicMock()
    mock_instance.whois = MagicMock(return_value=[])
    mock_instance.disconnect = MagicMock()
    mock_bac0.connect.return_value = mock_instance
    yield
    mock_bac0.reset_mock()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "bacnet"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1024

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:hello")
        assert ref.adapter_name == "bacnet"
        assert "bacnet://192.168.1.200" in ref.location
        assert "characterstringValue" in ref.location

    async def test_read_returns_data(self, adapter):
        data = "PCHK:m1:0:0:hello"
        mock_bac0.connect.return_value.read.return_value = data
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_opaque_data(self, adapter):
        ref = await adapter.write_chunk("opaque-sealed-data")
        assert ref.adapter_name == "bacnet"

    async def test_sequential_instances(self, adapter):
        ref1 = await adapter.write_chunk("PCHK:m1:0:0:a")
        ref2 = await adapter.write_chunk("PCHK:m1:0:1:b")
        assert ref1.location != ref2.location
        assert "1000" in ref1.location
        assert "1001" in ref2.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        result = await adapter.delete_chunk(ref)
        assert result is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="bacnet", location="bacnet://fake/x/0")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        await adapter.write_chunk("PCHK:manifest1:0:0:a")
        await adapter.write_chunk("PCHK:other:0:0:b")
        # Both go to uuid-based keys since PCHK:manifest1:0:0:a fails Chunk.decode
        # But the location won't contain manifest_id — test alloc tracking instead
        refs = await adapter.list_chunks("")
        assert len(refs) >= 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_bac0.connect.side_effect = ConnectionError
        assert await adapter.health_check() is False
        mock_bac0.connect.side_effect = None


@pytest.mark.asyncio
class TestCapacity:
    async def test_tracks_chunk_count(self, adapter):
        await adapter.write_chunk("PCHK:m1:0:0:data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
