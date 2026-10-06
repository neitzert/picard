"""Tests for the MongoDB adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.mongodb_adapter import MongoDBAdapter

_C1 = "PCHK:1:manifest1:0:0:1:aGVsbG8="
_C2 = "PCHK:1:manifest1:0:1:2:d29ybGQ="
_C3 = "PCHK:1:other:0:0:1:b3RoZXI="


@pytest.fixture
def adapter():
    return MongoDBAdapter(database="testdb", collection="testcoll")


class _FakeCollection:
    def __init__(self):
        self.store = {}
        self.database = MagicMock()
        self.database.client = MagicMock()

    async def replace_one(self, filter_, doc, upsert=False):
        self.store[filter_["_id"]] = doc

    async def find_one(self, filter_):
        return self.store.get(filter_["_id"])

    async def delete_one(self, filter_):
        result = MagicMock()
        if filter_["_id"] in self.store:
            del self.store[filter_["_id"]]
            result.deleted_count = 1
        else:
            result.deleted_count = 0
        return result

    def find(self, filter_):
        mid = filter_.get("manifest_id", "")
        results = [doc for doc in self.store.values() if doc.get("manifest_id") == mid]
        return _AsyncIter(results)

    async def count_documents(self, filter_):
        return len(self.store)


class _AsyncIter:
    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
class TestMongoDBAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "mongodb"
        assert p.persistent is True
        assert p.max_chunk_bytes == 16_777_216

    async def test_write_read_round_trip(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert ref.adapter_name == "mongodb"
        assert "mongo://" in ref.location
        result = await adapter.read_chunk(ref)
        assert result == _C1

    async def test_write_opaque_data(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = await adapter.write_chunk("not-a-pchk")
        result = await adapter.read_chunk(ref)
        assert result == "not-a-pchk"

    async def test_read_missing_raises(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = StoredChunkRef(adapter_name="mongodb", location="mongo://testdb/testcoll/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    async def test_delete_existing(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = StoredChunkRef(adapter_name="mongodb", location="mongo://testdb/testcoll/nope")
        assert await adapter.delete_chunk(ref) is False

    async def test_list_chunks(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        await adapter.write_chunk(_C1)
        await adapter.write_chunk(_C2)
        await adapter.write_chunk(_C3)
        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 2

    async def test_health_check_ok(self, adapter):
        fake = _FakeCollection()
        fake.database.client.server_info = AsyncMock(return_value={"version": "7.0"})
        adapter._get_collection = lambda: fake
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        fake = _FakeCollection()
        fake.database.client.server_info = AsyncMock(side_effect=Exception("nope"))
        adapter._get_collection = lambda: fake
        assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        fake = _FakeCollection()
        fake.database.command = AsyncMock(return_value={"dataSize": 1024, "storageSize": 4096})
        adapter._get_collection = lambda: fake
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 1024
        assert cap["chunk_count"] == 0

    async def test_location_format(self, adapter):
        fake = _FakeCollection()
        adapter._get_collection = lambda: fake
        ref = await adapter.write_chunk(_C1)
        assert ref.location.startswith("mongo://testdb/testcoll/")
