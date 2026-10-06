"""Tests for GitLabCIAdapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.gitlab_ci import GitLabCIAdapter


@pytest.fixture
def adapter():
    return GitLabCIAdapter(
        gitlab_url="https://gitlab.example.com",
        private_token="glpat-test",
        project_id=42,
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "gitlab-ci"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        resp = _FakeResponse(json_data={"url": "/uploads/abc123/file.pchk"})
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = await adapter.write_chunk("opaque-data")
        assert ref.adapter_name == "gitlab-ci"
        assert "42" in ref.location

    async def test_read_returns_data(self, adapter):
        resp = _FakeResponse(content=b"chunk-data")
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            ref = MagicMock()
            ref.location = "glci://42//uploads/abc123/file.pchk"
            data = await adapter.read_chunk(ref)
        assert data == "chunk-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_always_true(self, adapter):
        ref = MagicMock()
        ref.location = "glci://42/path"
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_returns_empty(self, adapter):
        refs = await adapter.list_chunks("any")
        assert refs == []


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        resp = _FakeResponse(status=200)
        with patch("aiohttp.ClientSession", return_value=_FakeSession(resp)):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("fail")):
            assert await adapter.health_check() is False
