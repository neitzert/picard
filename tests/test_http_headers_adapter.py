"""Tests for the HTTP headers adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.http_headers import HttpHeadersAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> HttpHeadersAdapter:
    defaults = dict(
        base_url="https://store.example.com",
        auth_token="tok-xxx",
    )
    defaults.update(kwargs)
    return HttpHeadersAdapter(**defaults)


def _mock_response(
    status_code: int = 200,
    json_data=None,
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or []
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
class TestHttpHeadersAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "http-headers"
        assert p.max_chunk_bytes == 7000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response(201)

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "http-headers"
        assert len(ref.location) == 16
        call_kwargs = client.post.call_args
        assert call_kwargs.args[0] == "https://store.example.com/store"
        hdrs = call_kwargs.kwargs["headers"]
        assert hdrs["X-PCHK-Data"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response(
            200, headers={"X-PCHK-Data": enc}
        )

        ref = StoredChunkRef(adapter_name="http-headers", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        client.get.assert_awaited_once_with(
            "https://store.example.com/retrieve/abc123"
        )

    async def test_read_chunk_missing_header(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, headers={})

        ref = StoredChunkRef(adapter_name="http-headers", location="abc123")
        with pytest.raises(ValueError, match="no X-PCHK-Data"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(204)

        ref = StoredChunkRef(adapter_name="http-headers", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(404)

        ref = StoredChunkRef(adapter_name="http-headers", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("network error")

        ref = StoredChunkRef(adapter_name="http-headers", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, json_data=["id1", "id2"])

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "http-headers" for r in refs)
        assert [r.location for r in refs] == ["id1", "id2"]
        call_kwargs = client.get.call_args
        assert call_kwargs.kwargs["params"] == {"manifest": "m1"}

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, json_data=[])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(500)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("unreachable")
        assert await a.health_check() is False

    async def test_no_auth_token(self):
        a = HttpHeadersAdapter(base_url="https://example.com")
        assert a._auth_token is None
        assert a._max == 7000

    async def test_base_url_trailing_slash_stripped(self):
        a = HttpHeadersAdapter(base_url="https://example.com/")
        assert a._base_url == "https://example.com"
