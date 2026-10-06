"""Tests for the SNMP adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.snmp import (
    SnmpAdapter,
    _chunk_oid,
    _manifest_oid_prefix,
)
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> SnmpAdapter:
    defaults = dict(host="127.0.0.1", port=10161)
    defaults.update(kwargs)
    return SnmpAdapter(**defaults)


class TestOidHelpers:
    def test_manifest_oid_prefix(self):
        prefix = _manifest_oid_prefix("1.3.6.1.4.1.99999.1", "manifest1")
        assert prefix == "1.3.6.1.4.1.99999.1.109.97.110.105.102.101.115.116"

    def test_chunk_oid(self):
        oid = _chunk_oid("1.3.6.1.4.1.99999.1", "m1", 2, 3)
        prefix = _manifest_oid_prefix("1.3.6.1.4.1.99999.1", "m1")
        assert oid == f"{prefix}.2.3"

    def test_short_manifest_id(self):
        prefix = _manifest_oid_prefix("1.3.6.1.4.1.99999.1", "ab")
        assert prefix == "1.3.6.1.4.1.99999.1.97.98"


@pytest.mark.asyncio
class TestSnmpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "snmp"
        assert p.max_chunk_bytes == 4000
        assert p.persistent is True
        assert p.rate_limit_per_min == 60

    async def test_write_chunk(self):
        a = _make_adapter()
        a._snmp_set = AsyncMock(return_value=True)
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "snmp"
        expected_oid = _chunk_oid(a._base_oid, "m1", 0, 0)
        assert ref.location == expected_oid
        a._snmp_set.assert_awaited_once_with(expected_oid, enc)

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._snmp_get = AsyncMock(return_value=enc)

        oid = _chunk_oid(a._base_oid, "m1", 0, 0)
        ref = StoredChunkRef(adapter_name="snmp", location=oid)
        result = await a.read_chunk(ref)

        assert result == enc
        a._snmp_get.assert_awaited_once_with(oid)

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._snmp_get = AsyncMock(return_value=None)

        ref = StoredChunkRef(adapter_name="snmp", location="1.3.6.1.99")
        with pytest.raises(ValueError, match="no value at OID"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._snmp_set = AsyncMock(return_value=True)

        ref = StoredChunkRef(adapter_name="snmp", location="1.3.6.1.99")
        assert await a.delete_chunk(ref) is True
        a._snmp_set.assert_awaited_once_with("1.3.6.1.99", "")

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._snmp_set = AsyncMock(side_effect=Exception("timeout"))

        ref = StoredChunkRef(adapter_name="snmp", location="1.3.6.1.99")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc = _sample_encoded()
        prefix = _manifest_oid_prefix(a._base_oid, "m1")
        entries = [
            (f"{prefix}.0.0", enc),
            (f"{prefix}.0.1", enc),
        ]
        a._snmp_walk = AsyncMock(return_value=entries)

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "snmp" for r in refs)
        a._snmp_walk.assert_awaited_once_with(prefix)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._snmp_walk = AsyncMock(return_value=[])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_filters_non_pchk(self):
        a = _make_adapter()
        prefix = _manifest_oid_prefix(a._base_oid, "m1")
        entries = [
            (f"{prefix}.0.0", _sample_encoded()),
            (f"{prefix}.0.1", "not a chunk"),
        ]
        a._snmp_walk = AsyncMock(return_value=entries)

        refs = await a.list_chunks("m1")
        assert len(refs) == 1

    async def test_list_chunks_error(self):
        a = _make_adapter()
        a._snmp_walk = AsyncMock(side_effect=Exception("unreachable"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._snmp_get = AsyncMock(return_value="Linux server 5.15.0")

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._snmp_get = AsyncMock(return_value=None)

        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._snmp_get = AsyncMock(side_effect=Exception("timeout"))

        assert await a.health_check() is False
