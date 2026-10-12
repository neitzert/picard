"""Tests for Fandom adapter — uses in-memory mock."""

from __future__ import annotations

import pytest

from picard.adapters.fandom_adapter import FandomAdapter


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self._pages: dict[str, str] = {}
        self.closed = False

    async def get(self, url, **kwargs):
        params = kwargs.get("params", {})
        action = params.get("action", "")
        if action == "query" and params.get("type") == "login":
            return _FakeResponse({"query": {"tokens": {"logintoken": "lt"}}})
        if action == "query" and params.get("meta") == "tokens":
            return _FakeResponse({"query": {"tokens": {"csrftoken": "csrf"}}})
        if action == "parse":
            page = params.get("page", "")
            return _FakeResponse({"parse": {"wikitext": {"*": self._pages.get(page, "")}}})
        if action == "query" and params.get("meta") == "siteinfo":
            return _FakeResponse({"query": {"general": {}}})
        return _FakeResponse({})

    async def post(self, url, **kwargs):
        data = kwargs.get("data", {})
        if data.get("action") == "login":
            return _FakeResponse({"login": {"result": "Success"}})
        if data.get("action") == "edit":
            self._pages[data.get("title", "")] = data.get("text", "")
            return _FakeResponse({"edit": {"result": "Success"}})
        return _FakeResponse({})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return FandomAdapter(
        wiki_domain="test.fandom.com",
        username="user",
        password="pass",
    )


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "fandom"

    def test_rate(self, adapter):
        assert adapter.profile().rate_limit_per_min == 8


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter, fake):
        adapter._session = fake
        ref = await adapter.write_chunk("fandomdata")
        assert ref.adapter_name == "fandom"
        result = await adapter.read_chunk(ref)
        assert result == "fandomdata"

    @pytest.mark.asyncio
    async def test_multi(self, adapter, fake):
        adapter._session = fake
        refs = [await adapter.write_chunk(f"f{i}") for i in range(3)]
        for i, ref in enumerate(refs):
            assert await adapter.read_chunk(ref) == f"f{i}"


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        adapter._session = fake
        ref = await adapter.write_chunk("gone")
        assert await adapter.delete_chunk(ref) is True


class TestList:
    @pytest.mark.asyncio
    async def test_list(self, adapter):
        adapter._cache["fandom://x/page1"] = "d"
        chunks = await adapter.list_chunks("x")
        assert len(chunks) == 1


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, adapter, fake):
        adapter._session = fake
        assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
