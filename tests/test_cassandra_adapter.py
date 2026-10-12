"""Tests for the Cassandra adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.cassandra_adapter import CassandraAdapter

_C1 = "PCHK:1:m1:0:0:1:YQ=="
_C2 = "PCHK:1:m1:0:1:2:Yg=="
_C3 = "PCHK:1:m2:0:0:1:Yw=="


@pytest.fixture
def adapter():
    return CassandraAdapter(keyspace="testks", table="testchunks")


class _FakeRow:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeSession:
    def __init__(self):
        self.store = {}

    def execute(self, query, params=None):
        q = query if isinstance(query, str) else str(query)
        if "INSERT INTO" in q:
            key = params[0]
            self.store[key] = {"key": key, "data": params[1], "manifest_id": params[2]}
            return None
        elif "SELECT data FROM" in q:
            key = params[0]
            if key in self.store:
                return MagicMock(one=lambda: _FakeRow(data=self.store[key]["data"]))
            return MagicMock(one=lambda: None)
        elif "DELETE FROM" in q:
            key = params[0]
            self.store.pop(key, None)
            return None
        elif "SELECT key FROM" in q:
            mid = params[0]
            return [_FakeRow(key=k) for k, v in self.store.items() if v["manifest_id"] == mid]
        elif "SELECT now()" in q:
            return MagicMock()
        elif "size_estimates" in q:
            return [_FakeRow(mean_partition_size=100, partitions_count=5)]
        return MagicMock()


@pytest.mark.asyncio
class TestCassandraAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "cassandra"
        assert p.persistent is True
        assert p.rate_limit_per_min == 10000

    async def test_write_read_round_trip(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert ref.adapter_name == "cassandra"
        result = await adapter.read_chunk(ref)
        assert result == _C1

    async def test_write_opaque_data(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        ref = await adapter.write_chunk("opaque-data")
        result = await adapter.read_chunk(ref)
        assert result == "opaque-data"

    async def test_read_missing_raises(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        ref = StoredChunkRef(adapter_name="cassandra", location="cassandra://testks/testchunks/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_delete(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert await adapter.delete_chunk(ref) is True

    async def test_list_chunks(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        await adapter.write_chunk(_C1)
        await adapter.write_chunk(_C2)
        await adapter.write_chunk(_C3)
        refs = await adapter.list_chunks("m1")
        assert len(refs) == 2

    async def test_health_check_ok(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        bad = MagicMock()
        bad.execute = MagicMock(side_effect=Exception("no cluster"))
        adapter._session = bad
        adapter._get_session = lambda: bad
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 500
        assert cap["chunk_count"] == 5

    async def test_location_format(self, adapter):
        fake = _FakeSession()
        adapter._session = fake
        adapter._get_session = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert ref.location.startswith("cassandra://testks/testchunks/")

    async def test_auth_config(self):
        a = CassandraAdapter(username="user", password="pass")
        assert a._username == "user"
