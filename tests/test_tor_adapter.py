"""Tests for the Tor adapter — mocked HTTP transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tor import TorAdapter


@pytest.fixture
def adapter():
    return TorAdapter(
        onion_url="http://abc123xyz.onion",
        adapter_name="test-fs",
        secret="test-secret",
        socks_proxy="socks5://127.0.0.1:9050",
    )


@pytest.fixture
def adapter_no_secret():
    return TorAdapter(
        onion_url="http://abc123xyz.onion",
        adapter_name="test-fs",
    )


def _mock_response(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


class TestProfile:
    def test_name_includes_onion(self, adapter):
        p = adapter.profile()
        assert "tor:" in p.name
        assert "abc123xyz.onion" in p.name

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes_default(self, adapter):
        assert adapter.profile().max_chunk_bytes == 64 * 1024

    def test_max_chunk_bytes_custom(self):
        a = TorAdapter("http://x.onion", "t", max_chunk_bytes=1024)
        assert a.profile().max_chunk_bytes == 1024

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30

    def test_tags(self, adapter):
        tags = adapter.profile().tags
        assert tags["tor"] == "true"
        assert "abc123xyz.onion" in tags["onion"]


class TestSign:
    def test_sign_with_secret(self, adapter):
        sig = adapter._sign(b"hello")
        assert "x-signature" in sig
        assert len(sig["x-signature"]) == 64

    def test_sign_without_secret(self, adapter_no_secret):
        assert adapter_no_secret._sign(b"hello") == {}

    def test_sign_deterministic(self, adapter):
        assert adapter._sign(b"p") == adapter._sign(b"p")

    def test_sign_differs_per_input(self, adapter):
        assert adapter._sign(b"a") != adapter._sign(b"b")


class TestWrite:
    async def test_write_returns_ref(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({
            "adapter": "test-fs",
            "location": "chunk://abc",
        })
        adapter._client = mock_client

        ref = await adapter.write_chunk("encoded-data")
        assert ref.adapter_name == "test-fs"
        assert ref.location == "chunk://abc"
        mock_client.post.assert_called_once()
        assert "/transport/v1/write_chunk" in str(mock_client.post.call_args)


class TestRead:
    async def test_read_returns_data(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({"data": "chunk-data"})
        adapter._client = mock_client

        ref = StoredChunkRef(adapter_name="test-fs", location="chunk://abc")
        assert await adapter.read_chunk(ref) == "chunk-data"


class TestDelete:
    async def test_delete_success(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({"deleted": True})
        adapter._client = mock_client

        ref = StoredChunkRef(adapter_name="test-fs", location="chunk://abc")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({"deleted": False})
        adapter._client = mock_client

        ref = StoredChunkRef(adapter_name="test-fs", location="chunk://nope")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    async def test_list_returns_refs(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({
            "refs": [
                {"adapter": "test-fs", "location": "chunk://1"},
                {"adapter": "test-fs", "location": "chunk://2"},
            ]
        })
        adapter._client = mock_client

        refs = await adapter.list_chunks("manifest-123")
        assert len(refs) == 2
        assert refs[0].location == "chunk://1"
        assert refs[1].location == "chunk://2"

    async def test_list_empty(self, adapter):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response({"refs": []})
        adapter._client = mock_client

        assert await adapter.list_chunks("empty") == []


class TestHealth:
    async def test_health_ok(self, adapter):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp
        adapter._client = mock_client

        assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("unreachable")
        adapter._client = mock_client

        assert await adapter.health_check() is False


class TestClose:
    async def test_close_client(self, adapter):
        mock_client = AsyncMock()
        adapter._client = mock_client

        await adapter.close()
        mock_client.aclose.assert_called_once()
        assert adapter._client is None

    async def test_close_noop_when_not_connected(self, adapter):
        await adapter.close()


class TestInit:
    def test_url_trailing_slash(self):
        a = TorAdapter("http://x.onion/", "t")
        assert a._onion_url == "http://x.onion"

    def test_default_timeout(self):
        a = TorAdapter("http://x.onion", "t")
        assert a._timeout == 120.0

    def test_default_socks_proxy(self):
        a = TorAdapter("http://x.onion", "t")
        assert a._socks_proxy == "socks5://127.0.0.1:9050"
