"""Tests for GitHubActionsAdapter."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.github_actions import GitHubActionsAdapter


@pytest.fixture
def adapter():
    return GitHubActionsAdapter(
        token="ghp_test", owner="testowner", repo="testrepo"
    )


class _FakeResponse:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status = status
        self._json = json_data or {}
        self._content = content

    async def json(self):
        return self._json

    async def read(self):
        return self._content

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

    def post(self, *a, **kw):
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
        assert adapter.profile().name == "github-actions"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        resp = _FakeResponse(json_data={"id": 12345})
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = await adapter.write_chunk("some-opaque-data")
        assert ref.adapter_name == "github-actions"
        assert "12345" in ref.location

    async def test_read_returns_data(self, adapter):
        encoded = base64.b64encode(b"test-chunk-data")
        resp = _FakeResponse(content=encoded)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "gha://testowner/testrepo/artifacts/12345"
            data = await adapter.read_chunk(ref)
        assert data == "test-chunk-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        resp = _FakeResponse(status=204)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "gha://testowner/testrepo/artifacts/12345"
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter):
        resp = _FakeResponse(status=404)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "gha://testowner/testrepo/artifacts/99999"
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        resp = _FakeResponse(
            json_data={
                "artifacts": [
                    {"id": 1, "name": "picard-abc123-b0-c0"},
                    {"id": 2, "name": "picard-abc123-b0-c1"},
                ]
            }
        )
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        resp = _FakeResponse(status=200)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            assert await adapter.health_check() is False
