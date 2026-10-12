"""Tests for the ClickHouse adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.clickhouse_adapter import ClickHouseAdapter

_C1 = "PCHK:1:mid:0:0:1:aGVsbG8="


@pytest.fixture
def adapter():
    return ClickHouseAdapter(host="testhost", database="testdb")


class _FakeResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeCH:
    def __init__(self):
        self.store = {}

    def handle(self, url, **kwargs):
        params = kwargs.get("params", {})
        query = params.get("query", "")
        data = kwargs.get("data", "")

        if "CREATE TABLE" in query:
            return _FakeResp(200)
        elif "INSERT INTO" in query:
            row = json.loads(data)
            self.store[row["key"]] = row
            return _FakeResp(200)
        elif "SELECT data FROM" in query:
            for key, row in self.store.items():
                if key in query:
                    return _FakeResp(200, text=json.dumps(row))
            return _FakeResp(200, text="")
        elif "DELETE WHERE" in query:
            return _FakeResp(200)
        elif "SELECT key FROM" in query:
            lines = []
            for key, row in self.store.items():
                if row.get("manifest_id", "") and row["manifest_id"] in query:
                    lines.append(json.dumps({"key": key}))
            return _FakeResp(200, text="\n".join(lines))
        elif "SELECT 1" in query:
            return _FakeResp(200, text="1")
        elif "count()" in query:
            r = {"cnt": len(self.store), "bytes": sum(len(v.get("data", "")) for v in self.store.values())}
            return _FakeResp(200, text=json.dumps(r))
        return _FakeResp(200)


def _mock_ch(ch):
    session = AsyncMock()
    session.closed = False
    session.post = lambda url, **kw: ch.handle(url, **kw)
    session.get = lambda url, **kw: ch.handle(url, **kw)
    return session


@pytest.mark.asyncio
class TestClickHouseAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "clickhouse"
        assert p.persistent is True
        assert p.max_chunk_bytes == 1_048_576

    async def test_write_read_round_trip(self, adapter):
        ch = _FakeCH()
        adapter._session = _mock_ch(ch)
        ref = await adapter.write_chunk(_C1)
        assert ref.adapter_name == "clickhouse"
        result = await adapter.read_chunk(ref)
        assert result == _C1

    async def test_health_check_ok(self, adapter):
        ch = _FakeCH()
        adapter._session = _mock_ch(ch)
        assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        session = AsyncMock()
        session.closed = False
        session.post = AsyncMock(side_effect=Exception("no"))
        session.get = AsyncMock(side_effect=Exception("no"))
        adapter._session = session
        assert await adapter.health_check() is False

    async def test_location_format(self, adapter):
        ch = _FakeCH()
        adapter._session = _mock_ch(ch)
        ref = await adapter.write_chunk(_C1)
        assert ref.location.startswith("clickhouse://testdb/chunks/")

    async def test_params_include_auth(self, adapter):
        params = adapter._params("SELECT 1")
        assert params["user"] == "default"
        assert params["database"] == "testdb"

    async def test_capacity_error_returns_none(self, adapter):
        session = AsyncMock()
        session.closed = False
        session.post = AsyncMock(side_effect=Exception("no"))
        session.get = AsyncMock(side_effect=Exception("no"))
        adapter._session = session
        cap = await adapter.capacity()
        assert cap["used_bytes"] is None

    async def test_delete(self, adapter):
        ch = _FakeCH()
        adapter._session = _mock_ch(ch)
        ref = StoredChunkRef(adapter_name="clickhouse", location="clickhouse://testdb/chunks/somekey")
        assert await adapter.delete_chunk(ref) is True

    async def test_max_chunk_bytes_configurable(self):
        a = ClickHouseAdapter(max_chunk_bytes=999)
        assert a.profile().max_chunk_bytes == 999

    async def test_write_opaque_data(self, adapter):
        ch = _FakeCH()
        adapter._session = _mock_ch(ch)
        ref = await adapter.write_chunk("opaque-sealed-data")
        result = await adapter.read_chunk(ref)
        assert result == "opaque-sealed-data"
