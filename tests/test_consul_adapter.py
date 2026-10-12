"""Tests for the Consul KV adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.consul import ConsulAdapter
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


def _make_adapter(**kwargs) -> ConsulAdapter:
    defaults = dict(endpoint="http://consul:8500", prefix="picard/")
    defaults.update(kwargs)
    return ConsulAdapter(**defaults)


def _mock_response(status_code: int = 200, text: str = "", json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestConsulAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "consul"
        assert p.max_chunk_bytes == 500_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 1000

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "picard/m1/b0_c2"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.put.return_value = _mock_response(200, text="true")

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "consul"
        assert ref.location == "picard/m1/b0_c0"
        client.put.assert_awaited_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response(200, text=enc)

        ref = StoredChunkRef(adapter_name="consul", location="picard/m1/b0_c0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(200)
        ref = StoredChunkRef(adapter_name="consul", location="picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(404)
        ref = StoredChunkRef(adapter_name="consul", location="picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("timeout")

        ref = StoredChunkRef(adapter_name="consul", location="picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(
            200,
            json_data=[
                "picard/m1/b0_c0",
                "picard/m1/b0_c1",
                "picard/m2/b0_c0",
            ],
        )

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "consul" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(404)
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, text='"10.0.0.1:8300"')
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, text="")
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("connection refused")
        assert await a.health_check() is False

    async def test_token_header(self):
        a = _make_adapter(token="my-secret-token")
        assert a._token == "my-secret-token"
