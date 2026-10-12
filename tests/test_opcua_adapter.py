"""Tests for OPC UA industrial automation adapter."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

mock_asyncua = MagicMock()
mock_asyncua.Client = MagicMock
mock_asyncua.ua = MagicMock()
mock_asyncua.ua.Variant = MagicMock()
mock_asyncua.ua.VariantType = MagicMock()
mock_asyncua.ua.VariantType.String = "String"
sys.modules.setdefault("asyncua", mock_asyncua)

from picard.adapters.opcua_adapter import OpcuaAdapter


@pytest.fixture
def adapter():
    return OpcuaAdapter("opc.tcp://192.168.1.50:4840")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "opcua"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 120


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.get_namespace_index = AsyncMock(return_value=2)
        mock_client.nodes = MagicMock()

        mock_var = AsyncMock()
        mock_var.write_value = AsyncMock()
        mock_client.get_node = MagicMock(return_value=mock_var)

        with patch("asyncua.Client", return_value=mock_client):
            with patch("asyncua.ua") as mock_ua:
                mock_ua.Variant = MagicMock()
                mock_ua.VariantType.String = "String"
                ref = await adapter.write_chunk("PCHK:m1:0:0:data")

        assert ref.adapter_name == "opcua"
        assert "opcua://" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.get_namespace_index = AsyncMock(return_value=2)
        mock_client.nodes = MagicMock()

        data = "PCHK:m1:0:0:testdata"

        mock_var = AsyncMock()
        mock_var.write_value = AsyncMock()
        mock_var.read_value = AsyncMock(return_value=data)
        mock_client.get_node = MagicMock(return_value=mock_var)

        with patch("asyncua.Client", return_value=mock_client):
            with patch("asyncua.ua") as mock_ua:
                mock_ua.Variant = MagicMock()
                mock_ua.VariantType.String = "String"
                ref = await adapter.write_chunk(data)

        with patch("asyncua.Client", return_value=mock_client):
            result = await adapter.read_chunk(ref)

        assert result == data

    async def test_opaque_data_uses_uuid(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.get_namespace_index = AsyncMock(return_value=2)
        mock_client.nodes = MagicMock()
        mock_var = AsyncMock()
        mock_var.write_value = AsyncMock()
        mock_client.get_node = MagicMock(return_value=mock_var)

        with patch("asyncua.Client", return_value=mock_client):
            with patch("asyncua.ua") as mock_ua:
                mock_ua.Variant = MagicMock()
                mock_ua.VariantType.String = "String"
                ref = await adapter.write_chunk("opaque-sealed-data-no-pchk")

        assert ref.adapter_name == "opcua"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.get_namespace_index = AsyncMock(return_value=2)
        mock_client.nodes = MagicMock()
        mock_var = AsyncMock()
        mock_var.write_value = AsyncMock()
        mock_client.get_node = MagicMock(return_value=mock_var)
        mock_client.delete_nodes = AsyncMock()

        with patch("asyncua.Client", return_value=mock_client):
            with patch("asyncua.ua") as mock_ua:
                mock_ua.Variant = MagicMock()
                mock_ua.VariantType.String = "String"
                ref = await adapter.write_chunk("PCHK:m1:0:0:data")
                result = await adapter.delete_chunk(ref)

        assert result is True

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="opcua", location="opcua://fake/ns=2;s=x")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_returns_all(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_client.get_namespace_index = AsyncMock(return_value=2)
        mock_client.nodes = MagicMock()
        mock_var = AsyncMock()
        mock_var.write_value = AsyncMock()
        mock_client.get_node = MagicMock(return_value=mock_var)

        with patch("asyncua.Client", return_value=mock_client):
            with patch("asyncua.ua") as mock_ua:
                mock_ua.Variant = MagicMock()
                mock_ua.VariantType.String = "String"
                await adapter.write_chunk("data1")
                await adapter.write_chunk("data2")

        refs = await adapter.list_chunks("")
        assert len(refs) >= 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_node = AsyncMock()
        mock_node.read_value = AsyncMock(return_value=0)
        mock_client.get_node = MagicMock(return_value=mock_node)

        with patch("asyncua.Client", return_value=mock_client):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock(side_effect=ConnectionRefusedError)

        with patch("asyncua.Client", return_value=mock_client):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
