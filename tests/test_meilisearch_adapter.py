"""Tests for the Meilisearch adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.meilisearch_adapter import MeilisearchAdapter

_C1 = "PCHK:1:m1:0:0:1:YQ=="
_C2 = "PCHK:1:m1:0:1:2:Yg=="
_ROUND = "PCHK:1:mid:0:0:1:aGVsbG8="


@pytest.fixture
def adapter():
    return MeilisearchAdapter(url="http://testhost:7700", index="test-idx")


class _FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeMeili:
    def __init__(self):
        self.store = {}

    def handle(self, method, url, **kwargs):
        if method == "POST" and "/documents" in url and "search" not in url:
            data = json.loads(kwargs.get("data", "[]"))
            for doc in data:
                self.store[doc["id"]] = doc
            return _FakeResponse(202, {"taskUid": 1})
        elif method == "GET" and "/documents/" in url:
            key = url.split("/documents/")[1]
            if key in self.store:
                return _FakeResponse(200, self.store[key])
            return _FakeResponse(404)
        elif method == "DELETE" and "/documents/" in url:
            key = url.split("/documents/")[1]
            if key in self.store:
                del self.store[key]
                return _FakeResponse(202)
            return _FakeResponse(404)
        elif method == "POST" and "/search" in url:
            query = json.loads(kwargs.get("data", "{}"))
            filt = query.get("filter", "")
            hits = []
            for doc in self.store.values():
                if doc.get("manifest_id", "") in filt:
                    hits.append(doc)
            return _FakeResponse(200, {"hits": hits})
        elif "/health" in url:
            return _FakeResponse(200, {"status": "available"})
        elif "/stats" in url:
            return _FakeResponse(
                200,
                {"numberOfDocuments": len(self.store), "rawDocumentDbSize": 2048},
            )
        return _FakeResponse(200)


def _mock_session(meili):
    session = AsyncMock()
    session.post = lambda url, **kw: meili.handle("POST", url, **kw)
    session.get = lambda url, **kw: meili.handle("GET", url, **kw)
    session.delete = lambda url, **kw: meili.handle("DELETE", url, **kw)
    return session


@pytest.mark.asyncio
class TestMeilisearchAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "meilisearch"
        assert p.persistent is True
        assert p.rate_limit_per_min == 1000

    async def test_write_read_round_trip(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert ref.adapter_name == "meilisearch"
            result = await adapter.read_chunk(ref)
            assert result == _ROUND

    async def test_read_missing_raises(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = StoredChunkRef(adapter_name="meilisearch", location="meili://test-idx/nope")
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)

    async def test_delete(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert await adapter.delete_chunk(ref) is True

    async def test_list_chunks(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter.write_chunk(_C1)
            await adapter.write_chunk(_C2)
            refs = await adapter.list_chunks("m1")
            assert len(refs) == 2

    async def test_health_check_ok(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(side_effect=Exception("no"))
            assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            cap = await adapter.capacity()
            assert cap["used_bytes"] == 2048
            assert cap["chunk_count"] == 0

    async def test_location_format(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert ref.location.startswith("meili://test-idx/")

    async def test_api_key_header(self):
        a = MeilisearchAdapter(api_key="test-key")
        h = a._headers()
        assert h["Authorization"] == "Bearer test-key"

    async def test_write_opaque_data(self, adapter):
        meili = _FakeMeili()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(meili))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk("opaque-sealed")
            result = await adapter.read_chunk(ref)
            assert result == "opaque-sealed"

    async def test_max_chunk_configurable(self):
        a = MeilisearchAdapter(max_chunk_bytes=999)
        assert a.profile().max_chunk_bytes == 999
