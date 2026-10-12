"""Tests for DNS-over-QUIC adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.doq_adapter import DoqAdapter


@pytest.fixture
def adapter():
    return DoqAdapter(zone="test.example.com", resolver="dns.adguard-dns.com")


class TestProfile:
    def test_profile_name(self, adapter):
        p = adapter.profile()
        assert p.name == "dns-over-quic"

    def test_profile_max_chunk(self, adapter):
        p = adapter.profile()
        assert p.max_chunk_bytes == 200

    def test_profile_not_ordered(self, adapter):
        p = adapter.profile()
        assert p.ordered is False

    def test_custom_max_chunk(self):
        a = DoqAdapter(zone="z.com", max_chunk_bytes=500)
        assert a.profile().max_chunk_bytes == 500


class TestFqdn:
    def test_fqdn_format(self, adapter):
        result = adapter._fqdn("mid123", 0, 1)
        assert result == "c1.b0.mid123.test.example.com"


class TestWriteChunk:
    @pytest.mark.asyncio
    async def test_write_returns_ref(self, adapter):
        adapter._quic_exchange = AsyncMock(return_value=b"\x00" * 12)
        ref = await adapter.write_chunk("test-data")
        assert ref.adapter_name == "dns-over-quic"
        assert "test.example.com" in ref.location

    @pytest.mark.asyncio
    async def test_write_with_chunk_format(self, adapter):
        adapter._quic_exchange = AsyncMock(return_value=b"\x00" * 12)
        from picard.chunker import Chunk
        chunk = Chunk(version=1, manifest_id="m1", block_index=0, chunk_index=0, total_chunks=1, payload=b"hello")
        ref = await adapter.write_chunk(chunk.encode())
        assert "m1" in ref.location


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_read_returns_data(self, adapter):
        mock_response = MagicMock()
        mock_rrset = MagicMock()
        mock_rdata = MagicMock()
        mock_rdata.strings = [b"test-data"]
        mock_rrset.__iter__ = lambda self: iter([mock_rdata])
        mock_response.answer = [mock_rrset]

        with patch("dns.message.from_wire", return_value=mock_response), \
             patch("dns.message.make_query") as mock_query:
            mock_query.return_value = MagicMock(to_wire=lambda: b"wire")
            adapter._quic_exchange = AsyncMock(return_value=b"\x00" * 12)
            from picard.adapters.base import StoredChunkRef
            ref = StoredChunkRef(adapter_name="dns-over-quic", location="c0.b0.test.test.example.com")
            result = await adapter.read_chunk(ref)
            assert result == "test-data"


class TestDeleteChunk:
    @pytest.mark.asyncio
    async def test_delete_success(self, adapter):
        adapter._quic_exchange = AsyncMock(return_value=b"\x00" * 12)
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="dns-over-quic", location="c0.b0.test.test.example.com")
        result = await adapter.delete_chunk(ref)
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_failure(self, adapter):
        adapter._quic_exchange = AsyncMock(side_effect=Exception("fail"))
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="dns-over-quic", location="c0.b0.test.test.example.com")
        result = await adapter.delete_chunk(ref)
        assert result is False


class TestListChunks:
    @pytest.mark.asyncio
    async def test_list_returns_empty(self, adapter):
        result = await adapter.list_chunks("any-id")
        assert result == []


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_success(self, adapter):
        adapter._quic_exchange = AsyncMock(return_value=b"\x00" * 12)
        assert await adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_failure(self, adapter):
        adapter._quic_exchange = AsyncMock(side_effect=Exception("fail"))
        assert await adapter.health_check() is False
