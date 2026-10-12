"""Tests for the Memcached adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.memcached import MemcachedAdapter
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


def _make_adapter(**kwargs) -> MemcachedAdapter:
    defaults = dict(host="localhost", port=11211)
    defaults.update(kwargs)
    return MemcachedAdapter(**defaults)


def _mock_reader(*responses: bytes) -> AsyncMock:
    reader = AsyncMock()
    reader.readline = AsyncMock(side_effect=responses)
    return reader


def _mock_writer() -> MagicMock:
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    return writer


@pytest.mark.asyncio
class TestMemcachedAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "memcached"
        assert p.max_chunk_bytes == 1_000_000
        assert p.persistent is False
        assert p.ttl_seconds == 3600

    async def test_profile_custom_ttl(self):
        a = _make_adapter(ttl_seconds=600)
        assert a.profile().ttl_seconds == 600

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "picard:m1:b0:c2"

    async def test_key_custom_prefix(self):
        a = _make_adapter(prefix="custom:")
        assert a._key("m1", 0, 0) == "custom:m1:b0:c0"

    async def test_write_chunk(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"STORED\r\n")
        a._reader = reader
        a._writer = writer

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "memcached"
        assert ref.location == "picard:m1:b0:c0"
        assert writer.write.called

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"SERVER_ERROR out of memory\r\n")
        a._reader = reader
        a._writer = writer

        with pytest.raises(RuntimeError, match="SET failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        data = enc.encode()
        writer = _mock_writer()
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=[
            f"VALUE picard:m1:b0:c0 0 {len(data)}\r\n".encode(),
            b"\r\n",
            b"END\r\n",
        ])
        reader.readexactly = AsyncMock(return_value=data)
        a._reader = reader
        a._writer = writer

        result = await a.read_chunk(
            StoredChunkRef(adapter_name="memcached", location="picard:m1:b0:c0")
        )
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"END\r\n")
        a._reader = reader
        a._writer = writer

        with pytest.raises(ValueError, match="key not found"):
            await a.read_chunk(
                StoredChunkRef(adapter_name="memcached", location="picard:m1:b0:c0")
            )

    async def test_delete_chunk(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"DELETED\r\n")
        a._reader = reader
        a._writer = writer

        ref = StoredChunkRef(adapter_name="memcached", location="picard:m1:b0:c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"NOT_FOUND\r\n")
        a._reader = reader
        a._writer = writer

        ref = StoredChunkRef(adapter_name="memcached", location="picard:m1:b0:c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"STAT pid 12345\r\n")
        a._reader = reader
        a._writer = writer

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        writer = _mock_writer()
        reader = _mock_reader(b"ERROR\r\n")
        a._reader = reader
        a._writer = writer

        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._writer = MagicMock()
        a._writer.write = MagicMock(side_effect=ConnectionError("refused"))
        a._writer.drain = AsyncMock()
        assert await a.health_check() is False
