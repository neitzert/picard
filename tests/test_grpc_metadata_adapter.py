"""Tests for the gRPC metadata adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.grpc_metadata import GrpcMetadataAdapter
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


def _make_adapter(**kwargs) -> GrpcMetadataAdapter:
    defaults = dict(endpoint="https://grpc-store.example.com")
    defaults.update(kwargs)
    return GrpcMetadataAdapter(**defaults)


def _mock_response(json_data=None, status_code: int = 200, headers=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestGrpcMetadataAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "grpc-metadata"
        assert p.max_chunk_bytes == 8000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({"id": "chunk-001"})

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "grpc-metadata"
        assert ref.location == "chunk-001"

        call_kwargs = client.post.call_args
        assert call_kwargs.kwargs["json"] == {"data": enc}
        assert call_kwargs.kwargs["headers"]["x-pchk-data"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response({"data": enc})

        ref = StoredChunkRef(adapter_name="grpc-metadata", location="chunk-001")
        result = await a.read_chunk(ref)
        assert result == enc

        client.get.assert_awaited_once_with(
            "https://grpc-store.example.com/v1/chunks/chunk-001"
        )

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(status_code=204)

        ref = StoredChunkRef(adapter_name="grpc-metadata", location="chunk-001")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(status_code=404)

        ref = StoredChunkRef(adapter_name="grpc-metadata", location="chunk-001")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="grpc-metadata", location="chunk-001")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(
            {"ids": ["c1", "c2", "c3"]}
        )

        refs = await a.list_chunks("m1")
        assert len(refs) == 3
        assert all(r.adapter_name == "grpc-metadata" for r in refs)
        assert [r.location for r in refs] == ["c1", "c2", "c3"]

        client.get.assert_awaited_once_with(
            "https://grpc-store.example.com/v1/chunks",
            params={"manifest": "m1"},
        )

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"ids": []})

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(status_code=200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(status_code=503)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False

    async def test_api_key_header(self):
        a = _make_adapter(api_key="secret-key-123")
        await a._ensure_client()
        assert a._client is not None

    async def test_endpoint_trailing_slash(self):
        a = _make_adapter(endpoint="https://grpc-store.example.com/")
        assert a._endpoint == "https://grpc-store.example.com"
