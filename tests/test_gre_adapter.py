"""Tests for the GRE adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.gre_adapter import GreAdapter


@pytest.fixture
def adapter():
    return GreAdapter(
        tunnel_src="10.0.0.1",
        tunnel_dst="10.0.0.2",
        gre_key=0x91CA4D,
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "gre"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_ordered(self, adapter):
        assert adapter.profile().ordered is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8950


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_and_read(self, adapter):
        with patch("picard.adapters.gre_adapter.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_socket.socket.return_value = mock_sock
            mock_socket.AF_INET = 2
            mock_socket.SOCK_RAW = 3
            ref = await adapter.write_chunk("gre-test-data")

        assert ref.adapter_name == "gre"
        assert "gre://" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == "gre-test-data"

    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="gre", location="gre://10.0.0.2/0x0/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_sequence_increments(self, adapter):
        with patch("picard.adapters.gre_adapter.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_socket.socket.return_value = mock_sock
            mock_socket.AF_INET = 2
            mock_socket.SOCK_RAW = 3
            ref1 = await adapter.write_chunk("chunk1")
            ref2 = await adapter.write_chunk("chunk2")

        assert ref1.location != ref2.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        with patch("picard.adapters.gre_adapter.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_socket.socket.return_value = mock_sock
            mock_socket.AF_INET = 2
            mock_socket.SOCK_RAW = 3
            ref = await adapter.write_chunk("delete-me")

        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="gre", location="gre://x/y/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        with patch("picard.adapters.gre_adapter.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_socket.socket.return_value = mock_sock
            mock_socket.AF_INET = 2
            mock_socket.SOCK_RAW = 3
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("picard.adapters.gre_adapter.socket") as mock_socket:
            mock_socket.socket.side_effect = OSError("no raw")
            mock_socket.AF_INET = 2
            mock_socket.SOCK_RAW = 3
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0

    async def test_with_data(self, adapter):
        adapter._cache["k1"] = "hello-gre"
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
