"""Tests for SAML adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from picard.adapters.saml_adapter import SamlAdapter


@pytest.fixture
def adapter():
    return SamlAdapter(
        idp_url="https://idp.example.com",
        sp_entity_id="https://sp.example.com/saml",
    )


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "saml"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 4096

    def test_profile_not_persistent(self, adapter):
        p = adapter.profile()
        assert p.persistent is False

    def test_profile_ttl(self, adapter):
        p = adapter.profile()
        assert p.ttl_seconds == 3600


class TestBuildAssertion:
    def test_builds_valid_xml(self, adapter):
        xml = adapter._build_assertion("key1", "test-data")
        assert "picard:chunk" in xml
        assert "key1" in xml
        assert "<saml:Assertion" in xml or "Assertion" in xml


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "saml"
        assert "saml://" in ref.location

    @pytest.mark.asyncio
    async def test_write_stores_in_cache(self, adapter):
        ref = await adapter.write_chunk("test-data")
        key = ref.location.rsplit("/", 1)[-1]
        assert key in adapter._cache

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        from picard.chunker import Chunk
        chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"hello")
        ref = await adapter.write_chunk(chunk.encode())
        assert "m1_0_0" in ref.location


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_read_returns_data(self, adapter):
        ref = await adapter.write_chunk("test-data")
        result = await adapter.read_chunk(ref)
        assert result == "test-data"

    @pytest.mark.asyncio
    async def test_read_missing_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="saml", location="saml://sp/missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("test-data")
        assert await adapter.delete_chunk(ref) is True

    @pytest.mark.asyncio
    async def test_delete_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="saml", location="saml://sp/missing")
        assert await adapter.delete_chunk(ref) is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_filters_by_manifest(self, adapter):
        from picard.chunker import Chunk
        c1 = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=2, payload=b"a")
        c2 = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=1, total_chunks=2, payload=b"b")
        c3 = Chunk(version=1, manifest_id="m2", block_index=0, chunk_index=0, total_chunks=1, payload=b"c")
        await adapter.write_chunk(c1.encode())
        await adapter.write_chunk(c2.encode())
        await adapter.write_chunk(c3.encode())
        result = await adapter.list_chunks("m1")
        assert len(result) == 2


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_no_key(self, adapter):
        assert await adapter.health_check() is True


class TestCapacity:
    @pytest.mark.asyncio
    async def test_capacity_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0

    @pytest.mark.asyncio
    async def test_capacity_after_write(self, adapter):
        await adapter.write_chunk("test-data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
