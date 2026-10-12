"""Tests for the syslog adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.syslog_adapter import SyslogAdapter, _extract_sd_field


@pytest.fixture
def adapter():
    return SyslogAdapter(
        "loghost.local",
        port=514,
        protocol="udp",
        read_api_url="http://es:9200/syslog",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "syslog"

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 2048

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            ref = await adapter.write_chunk("PCHK:1:test123:0:0:1:dGVzdA==")
            assert ref.adapter_name == "syslog"
            assert ref.location.startswith("syslog://loghost.local/")

    async def test_round_trip_from_cache(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            ref = await adapter.write_chunk("PCHK:1:test123:0:0:1:dGVzdA==")
            result = await adapter.read_chunk(ref)
            assert result == "PCHK:1:test123:0:0:1:dGVzdA=="

    async def test_read_missing_raises(self):
        a = SyslogAdapter("loghost.local", port=514, protocol="udp")
        ref = StoredChunkRef(adapter_name="syslog", location="syslog://loghost.local/nope")
        with pytest.raises((KeyError, LookupError)):
            await a.read_chunk(ref)

    async def test_opaque_data_uses_uuid(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            ref = await adapter.write_chunk("not-a-valid-chunk")
            assert ref.adapter_name == "syslog"
            result = await adapter.read_chunk(ref)
            assert result == "not-a-valid-chunk"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            ref = await adapter.write_chunk("PCHK:1:del:0:0:1:dGVzdA==")
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_nonexistent(self, adapter):
        ref = StoredChunkRef(adapter_name="syslog", location="syslog://loghost.local/nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            await adapter.write_chunk("PCHK:1:manifest1:0:0:1:YQ==")
            await adapter.write_chunk("PCHK:1:manifest1:1:0:1:Yg==")
            await adapter.write_chunk("PCHK:1:other:0:0:1:Yw==")
            refs = await adapter.list_chunks("manifest1")
            assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch.object(adapter, "_send_syslog", side_effect=OSError("refused")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    async def test_after_writes(self, adapter):
        with patch.object(adapter, "_send_syslog"):
            await adapter.write_chunk("PCHK:1:cap:0:0:1:dGVzdA==")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
            assert cap["used_bytes"] > 0


class TestExtractSdField:
    def test_extracts_data(self):
        sd = '[picard@0 msgid="test" data="cGF5bG9hZA=="]'
        assert _extract_sd_field(sd, "data") == "cGF5bG9hZA=="

    def test_missing_field_raises(self):
        with pytest.raises(ValueError):
            _extract_sd_field('[foo@0 x="1"]', "data")
