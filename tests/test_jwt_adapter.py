"""Tests for JWT adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from picard.adapters.jwt_adapter import JwtAdapter


@pytest.fixture
def adapter():
    return JwtAdapter(secret="test-secret-key", issuer="test-picard")


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "jwt"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 4096

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False

    def test_profile_ttl(self, adapter):
        p = adapter.profile()
        assert p.ttl_seconds == 3600


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "jwt"
        assert ref.location

    @pytest.mark.asyncio
    async def test_write_stores_jwt(self, adapter):
        ref = await adapter.write_chunk("test-data")
        assert ref.location in adapter._cache
        token = adapter._cache[ref.location]
        assert token.count(".") == 2  # JWT has 3 parts

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        from picard.chunker import Chunk
        chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"hello")
        ref = await adapter.write_chunk(chunk.encode())
        assert ref.location == "m1_0_0"


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_roundtrip(self, adapter):
        ref = await adapter.write_chunk("secret-payload")
        result = await adapter.read_chunk(ref)
        assert result == "secret-payload"

    @pytest.mark.asyncio
    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="jwt", location="missing-key")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)

    @pytest.mark.asyncio
    async def test_roundtrip_binary_safe(self, adapter):
        import base64
        data = base64.b64encode(b"\x00\x01\xff").decode()
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("data")
        assert await adapter.delete_chunk(ref) is True
        assert ref.location not in adapter._cache

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="jwt", location="missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters_by_manifest(self, adapter):
        from picard.chunker import Chunk
        c1 = Chunk(manifest_id="m1", block_index=0, chunk_index=0, data=b"a")
        c2 = Chunk(manifest_id="m1", block_index=0, chunk_index=1, data=b"b")
        c3 = Chunk(manifest_id="m2", block_index=0, chunk_index=0, data=b"c")
        await adapter.write_chunk(c1.encode())
        await adapter.write_chunk(c2.encode())
        await adapter.write_chunk(c3.encode())
        result = await adapter.list_chunks("m1")
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_empty(self, adapter):
        result = await adapter.list_chunks("nonexistent")
        assert result == []


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check(self, adapter):
        assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_capacity_after_writes(self, adapter):
        await adapter.write_chunk("data1")
        await adapter.write_chunk("data2")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 2
        assert cap["used_bytes"] > 0
