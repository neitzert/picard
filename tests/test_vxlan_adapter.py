"""Tests for the VXLAN adapter."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from picard.adapters.vxlan_adapter import VxlanAdapter


@pytest.fixture
def adapter():
    return VxlanAdapter(
        vtep_ip="10.0.0.1",
        dest_vtep_ip="10.0.0.2",
        vni=999999,
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "vxlan"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8950


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_and_read(self, adapter):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            ref = await adapter.write_chunk("vxlan-test-data")

        assert ref.adapter_name == "vxlan"
        assert "vxlan://" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == "vxlan-test-data"

    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="vxlan", location="vxlan://10.0.0.2:999999/missing"
        )
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            ref = await adapter.write_chunk("to-delete")

        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="vxlan", location="vxlan://10.0.0.2:999999/nope"
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter):
        adapter._cache["manifest123_0_0"] = "data0"
        adapter._cache["manifest123_0_1"] = "data1"
        adapter._cache["other_0_0"] = "other"

        refs = await adapter.list_chunks("manifest123")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("socket.socket", side_effect=OSError("no raw")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0

    async def test_with_data(self, adapter):
        adapter._cache["k1"] = "hello"
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 5
        assert cap["chunk_count"] == 1
