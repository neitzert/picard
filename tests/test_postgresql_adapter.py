"""Tests for the PostgreSQL adapter -- mocked, no database calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.postgresql import PostgresqlAdapter
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


def _make_adapter(**kwargs) -> PostgresqlAdapter:
    defaults = dict(dsn="postgresql://user:pass@localhost/picard")
    defaults.update(kwargs)
    return PostgresqlAdapter(**defaults)


def _mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.mark.asyncio
class TestPostgresqlAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "postgresql"
        assert p.max_chunk_bytes == 100_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 5000

    async def test_write_chunk(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "postgresql"
        assert len(ref.location) == 32  # uuid hex
        conn.execute.assert_awaited_once()
        call_args = conn.execute.call_args
        assert "INSERT INTO" in call_args[0][0]

    async def test_read_chunk(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool
        enc = _sample_encoded()

        conn.fetchrow.return_value = {"data": enc}

        ref = StoredChunkRef(adapter_name="postgresql", location="abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.fetchrow.return_value = None

        ref = StoredChunkRef(adapter_name="postgresql", location="missing")
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.execute.return_value = "DELETE 1"

        ref = StoredChunkRef(adapter_name="postgresql", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.execute.return_value = "DELETE 0"

        ref = StoredChunkRef(adapter_name="postgresql", location="missing")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool
        conn.execute.side_effect = Exception("db error")

        ref = StoredChunkRef(adapter_name="postgresql", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.fetch.return_value = [
            {"id": "aaa"},
            {"id": "bbb"},
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert refs[0].location == "aaa"
        assert refs[1].location == "bbb"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.fetch.return_value = []
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool

        conn.fetchval.return_value = 1
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        pool, conn = _mock_pool()
        a._pool = pool
        conn.fetchval.side_effect = Exception("connection lost")
        assert await a.health_check() is False
