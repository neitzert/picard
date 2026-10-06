"""Tests for the LLDP adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.lldp_adapter import LldpAdapter


@pytest.fixture
def adapter():
    return LldpAdapter("eth0", oui="aabbcc")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "lldp"

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 507

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 2

    def test_ttl(self, adapter):
        assert adapter.profile().ttl_seconds == 120


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:lldp1:0:0:1:dGVzdA==")
                assert ref.adapter_name == "lldp"
                assert ref.location.startswith("lldp://eth0/")

    async def test_round_trip(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:lldp1:0:0:1:dGVzdA==")
                result = await adapter.read_chunk(ref)
                assert result == "PCHK:1:lldp1:0:0:1:dGVzdA=="

    async def test_read_missing_raises(self, adapter):
        ref = StoredChunkRef(adapter_name="lldp", location="lldp://eth0/1/nope")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_opaque_data(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                ref = await adapter.write_chunk("sealed-envelope-hex")
                result = await adapter.read_chunk(ref)
                assert result == "sealed-envelope-hex"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                ref = await adapter.write_chunk("PCHK:1:d:0:0:1:dGVzdA==")
                assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(adapter_name="lldp", location="lldp://eth0/1/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                await adapter.write_chunk("PCHK:1:mf1:0:0:1:YQ==")
                await adapter.write_chunk("PCHK:1:mf1:1:0:1:Yg==")
                await adapter.write_chunk("PCHK:1:other:0:0:1:Yw==")
                refs = await adapter.list_chunks("mf1")
                assert len(refs) == 2

    async def test_list_empty(self, adapter):
        refs = await adapter.list_chunks("nothing")
        assert refs == []


@pytest.mark.asyncio
class TestHealth:
    async def test_health_fallback(self, adapter):
        with patch.dict("sys.modules", {"psutil": None}):
            result = await adapter.health_check()
            assert result is True


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_after_writes(self, adapter):
        with patch.object(adapter, "_send_frame"):
            with patch.object(adapter, "_build_frame", return_value=MagicMock()):
                await adapter.write_chunk("PCHK:1:c:0:0:1:dGVzdA==")
                cap = await adapter.capacity()
                assert cap["chunk_count"] == 1
                assert cap["used_bytes"] > 0
