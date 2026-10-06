"""Tests for the Neo4j adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.neo4j_adapter import Neo4jAdapter

_C1 = "PCHK:1:m1:0:0:1:YQ=="
_C2 = "PCHK:1:m1:0:1:2:Yg=="
_C3 = "PCHK:1:m2:0:0:1:Yw=="


@pytest.fixture
def adapter():
    return Neo4jAdapter(database="testdb", password="testpass")


class _FakeSession:
    def __init__(self, store):
        self._store = store

    def run(self, query, **kwargs):
        if "MERGE" in query or "SET" in query:
            key = kwargs.get("key")
            self._store[key] = {
                "data": kwargs.get("data", ""),
                "manifest_id": kwargs.get("mid", ""),
            }
            return MagicMock()
        elif "RETURN c.data" in query:
            key = kwargs.get("key")
            if key in self._store:
                record = {"data": self._store[key]["data"]}
                return MagicMock(single=lambda: record)
            return MagicMock(single=lambda: None)
        elif "DELETE c" in query:
            key = kwargs.get("key")
            if key in self._store:
                del self._store[key]
                return MagicMock(single=lambda: {"cnt": 1})
            return MagicMock(single=lambda: {"cnt": 0})
        elif "RETURN c.key" in query:
            mid = kwargs.get("mid", "")
            results = [
                {"key": k} for k, v in self._store.items() if v["manifest_id"] == mid
            ]
            return results
        elif "count(c)" in query:
            return MagicMock(single=lambda: {"cnt": len(self._store)})
        elif "RETURN 1" in query:
            return MagicMock()
        return MagicMock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeDriver:
    def __init__(self):
        self._store = {}

    def session(self, database=None):
        return _FakeSession(self._store)


@pytest.mark.asyncio
class TestNeo4jAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "neo4j"
        assert p.persistent is True
        assert p.rate_limit_per_min == 1000

    async def test_write_read_round_trip(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = await adapter.write_chunk(_C1)
        assert ref.adapter_name == "neo4j"
        result = await adapter.read_chunk(ref)
        assert result == _C1

    async def test_write_opaque_data(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = await adapter.write_chunk("opaque-stuff")
        result = await adapter.read_chunk(ref)
        assert result == "opaque-stuff"

    async def test_read_missing_raises(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = StoredChunkRef(adapter_name="neo4j", location="neo4j://testdb/Chunk/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_delete_existing(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = await adapter.write_chunk(_C1)
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = StoredChunkRef(adapter_name="neo4j", location="neo4j://testdb/Chunk/nope")
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        await adapter.write_chunk(_C1)
        await adapter.write_chunk(_C2)
        await adapter.write_chunk(_C3)
        refs = await adapter.list_chunks("m1")
        assert len(refs) == 2

    async def test_health_check_ok(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        bad = MagicMock()
        bad.session.return_value.__enter__ = MagicMock(
            side_effect=Exception("no connection")
        )
        adapter._driver = bad
        adapter._get_driver = lambda: bad
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        await adapter.write_chunk(_C1)
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1

    async def test_location_format(self, adapter):
        adapter._driver = _FakeDriver()
        adapter._get_driver = lambda: adapter._driver
        ref = await adapter.write_chunk(_C1)
        assert ref.location.startswith("neo4j://testdb/Chunk/")
