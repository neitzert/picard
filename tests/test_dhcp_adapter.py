"""Tests for the DHCP adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.dhcp_adapter import DhcpAdapter


@pytest.fixture
def adapter():
    return DhcpAdapter("eth0", server_ip="192.168.1.1", option_code=224)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "dhcp"

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 255

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:test1:0:0:1:dGVzdA==")
                assert ref.adapter_name == "dhcp"
                assert ref.location.startswith("dhcp://eth0/224/")

    async def test_round_trip(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:test1:0:0:1:dGVzdA==")
                result = await adapter.read_chunk(ref)
                assert result == "PCHK:1:test1:0:0:1:dGVzdA=="

    async def test_read_missing_raises(self, adapter):
        ref = StoredChunkRef(adapter_name="dhcp", location="dhcp://eth0/224/nope")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_opaque_data(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                ref = await adapter.write_chunk("sealed-hex-data")
                result = await adapter.read_chunk(ref)
                assert result == "sealed-hex-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:del:0:0:1:dGVzdA==")
                assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(adapter_name="dhcp", location="dhcp://eth0/224/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                await adapter.write_chunk("PCHK:1:mf1:0:0:1:YQ==")
                await adapter.write_chunk("PCHK:1:mf1:1:0:1:Yg==")
                await adapter.write_chunk("PCHK:1:other:0:0:1:Yw==")
                refs = await adapter.list_chunks("mf1")
                assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_health_with_psutil(self, adapter):
        mock_addrs = {"eth0": [], "lo": []}
        with patch("picard.adapters.dhcp_adapter.DhcpAdapter.health_check") as mock:
            mock.return_value = True
            assert await adapter.health_check() is True

    async def test_health_missing_iface(self, adapter):
        with patch.dict("sys.modules", {"psutil": MagicMock()}):
            import sys

            sys.modules["psutil"].net_if_addrs.return_value = {"lo": []}
            result = await adapter.health_check()
            assert result is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["free_bytes"] is None

    async def test_after_writes(self, adapter):
        with patch.object(adapter, "_send_packet"):
            with patch.object(adapter, "_build_inform", return_value=MagicMock()):
                await adapter.write_chunk("PCHK:1:c:0:0:1:dGVzdA==")
                cap = await adapter.capacity()
                assert cap["chunk_count"] == 1
                assert cap["used_bytes"] > 0
