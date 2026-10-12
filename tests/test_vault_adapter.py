"""Tests for VaultAdapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.vault_adapter import VaultAdapter


@pytest.fixture
def adapter():
    return VaultAdapter(
        token="hvs.test-token", address="http://vault.test:8200"
    )


class _FakeResponse:
    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json = json_data or {}

    async def json(self):
        return self._json

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def put(self, *a, **kw):
        return self._response

    def get(self, *a, **kw):
        return self._response

    def delete(self, *a, **kw):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "vault"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 500

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        resp = _FakeResponse(status=200)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = await adapter.write_chunk("opaque-data")
        assert ref.adapter_name == "vault"
        assert "vault://" in ref.location

    async def test_read_returns_data(self, adapter):
        resp = _FakeResponse(json_data={
            "data": {"data": {"chunk": "secret-chunk"}}
        })
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "vault://secret/picard/abc-b0-c0"
            data = await adapter.read_chunk(ref)
        assert data == "secret-chunk"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        resp = _FakeResponse(status=204)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "vault://secret/picard/abc"
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_fail(self, adapter):
        resp = _FakeResponse(status=404)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "vault://secret/picard/nope"
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        resp = _FakeResponse(json_data={
            "data": {"keys": ["abc-b0-c0", "abc-b0-c1", "xyz-b0-c0"]}
        })
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            refs = await adapter.list_chunks("abc")
        assert len(refs) == 2

    async def test_list_empty(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            refs = await adapter.list_chunks("nope")
        assert refs == []


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        resp = _FakeResponse(status=200)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("sealed")):
            assert await adapter.health_check() is False
