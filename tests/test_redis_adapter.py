"""Tests for the Redis adapter -- mocked, no Redis connection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.redis_adapter import RedisAdapter
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


def _make_adapter(**kwargs) -> RedisAdapter:
    defaults = dict(url="redis://localhost:6379", prefix="picard:")
    defaults.update(kwargs)
    return RedisAdapter(**defaults)


@pytest.mark.asyncio
class TestRedisAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "redis"
        assert p.max_chunk_bytes == 500_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 10000
        assert p.ttl_seconds is None

    async def test_profile_with_ttl(self):
        a = _make_adapter(ttl_seconds=3600)
        p = a.profile()
        assert p.ttl_seconds == 3600

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "picard:m1:b0:c2"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "redis"
        assert ref.location == "picard:m1:b0:c0"
        client.set.assert_awaited_once_with("picard:m1:b0:c0", enc)

    async def test_write_chunk_with_ttl(self):
        a = _make_adapter(ttl_seconds=600)
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        client.set.assert_awaited_once_with("picard:m1:b0:c0", enc, ex=600)

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()
        client.get.return_value = enc.encode()

        ref = StoredChunkRef(adapter_name="redis", location="picard:m1:b0:c0")
        result = await a.read_chunk(ref)

        assert result == enc
        client.get.assert_awaited_once_with("picard:m1:b0:c0")

    async def test_read_chunk_string_value(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()
        client.get.return_value = enc  # string, not bytes

        ref = StoredChunkRef(adapter_name="redis", location="picard:m1:b0:c0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.return_value = None

        ref = StoredChunkRef(adapter_name="redis", location="picard:m1:b0:c0")
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.return_value = 1

        ref = StoredChunkRef(adapter_name="redis", location="picard:m1:b0:c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.return_value = 0

        ref = StoredChunkRef(adapter_name="redis", location="picard:m1:b0:c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.keys.return_value = [
            b"picard:m1:b0:c0",
            b"picard:m1:b0:c1",
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "redis" for r in refs)
        client.keys.assert_awaited_once_with("picard:m1:*")

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.keys.return_value = []

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.ping.return_value = True

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.ping.side_effect = Exception("connection refused")

        assert await a.health_check() is False
