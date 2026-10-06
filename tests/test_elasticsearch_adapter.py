"""Tests for the Elasticsearch adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.elasticsearch_adapter import ElasticsearchAdapter

_C1 = "PCHK:1:m1:0:0:1:YQ=="
_C2 = "PCHK:1:m1:0:1:2:Yg=="
_ROUND = "PCHK:1:mid:0:0:1:aGVsbG8="


@pytest.fixture
def adapter():
    return ElasticsearchAdapter(
        hosts=["http://testhost:9200"], index="test-index"
    )


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


class _FakeES:
    def __init__(self):
        self.store = {}

    def handle(self, method, url, **kwargs):
        if method == "PUT" and "/_doc/" in url:
            key = url.split("/_doc/")[1]
            data = json.loads(kwargs.get("data", "{}"))
            self.store[key] = data
            return _FakeResponse(201, {"result": "created"})
        elif method == "GET" and "/_doc/" in url:
            key = url.split("/_doc/")[1]
            if key in self.store:
                return _FakeResponse(200, {"_source": self.store[key]})
            return _FakeResponse(404, {"found": False})
        elif method == "DELETE" and "/_doc/" in url:
            key = url.split("/_doc/")[1]
            if key in self.store:
                del self.store[key]
                return _FakeResponse(200)
            return _FakeResponse(404)
        elif method == "POST" and "/_search" in url:
            query = json.loads(kwargs.get("data", "{}"))
            mid = query.get("query", {}).get("term", {}).get("manifest_id", "")
            hits = [
                {"_id": k}
                for k, v in self.store.items()
                if v.get("manifest_id") == mid
            ]
            return _FakeResponse(200, {"hits": {"hits": hits}})
        elif "/_cluster/health" in url:
            return _FakeResponse(200, {"status": "green"})
        elif "/_stats" in url:
            return _FakeResponse(
                200,
                {
                    "_all": {
                        "total": {
                            "store": {"size_in_bytes": 1024},
                            "docs": {"count": len(self.store)},
                        }
                    }
                },
            )
        return _FakeResponse(200)


def _mock_session(es):
    session = AsyncMock()
    session.put = lambda url, **kw: es.handle("PUT", url, **kw)
    session.get = lambda url, **kw: es.handle("GET", url, **kw)
    session.delete = lambda url, **kw: es.handle("DELETE", url, **kw)
    session.post = lambda url, **kw: es.handle("POST", url, **kw)
    return session


@pytest.mark.asyncio
class TestElasticsearchAdapter:
    def test_profile(self, adapter):
        p = adapter.profile()
        assert p.name == "elasticsearch"
        assert p.persistent is True
        assert p.rate_limit_per_min == 3000

    async def test_write_read_round_trip(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert ref.adapter_name == "elasticsearch"
            result = await adapter.read_chunk(ref)
            assert result == _ROUND

    async def test_read_missing_raises(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = StoredChunkRef(adapter_name="elasticsearch", location="es://test-index/nope")
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)

    async def test_delete(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert await adapter.delete_chunk(ref) is True

    async def test_list_chunks(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            await adapter.write_chunk(_C1)
            await adapter.write_chunk(_C2)
            refs = await adapter.list_chunks("m1")
            assert len(refs) == 2

    async def test_health_check_ok(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            assert await adapter.health_check() is True

    async def test_health_check_fail(self, adapter):
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(side_effect=Exception("no"))
            assert await adapter.health_check() is False

    async def test_capacity(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            cap = await adapter.capacity()
            assert cap["used_bytes"] == 1024

    async def test_location_format(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk(_ROUND)
            assert ref.location.startswith("es://test-index/")

    async def test_api_key_header(self):
        a = ElasticsearchAdapter(api_key="test-key-123")
        h = a._headers()
        assert h["Authorization"] == "ApiKey test-key-123"

    async def test_basic_auth(self):
        a = ElasticsearchAdapter(username="user", password="pass")
        auth = a._auth()
        assert auth is not None
        assert auth.login == "user"

    async def test_write_opaque_data(self, adapter):
        es = _FakeES()
        with patch("aiohttp.ClientSession") as mock_cs:
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=_mock_session(es))
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
            ref = await adapter.write_chunk("opaque-sealed")
            result = await adapter.read_chunk(ref)
            assert result == "opaque-sealed"
