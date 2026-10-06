"""Tests for the SQLite remote adapter -- mocked, no real database."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sqlite_remote import SqliteRemoteAdapter
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


def _make_adapter(**kwargs) -> SqliteRemoteAdapter:
    defaults = dict(db_path=":memory:")
    defaults.update(kwargs)
    return SqliteRemoteAdapter(**defaults)


def _mock_cursor(fetchone=None, fetchall=None, rowcount=0):
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=fetchone)
    cursor.fetchall = AsyncMock(return_value=fetchall or [])
    cursor.rowcount = rowcount
    return cursor


@pytest.mark.asyncio
class TestSqliteRemoteAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "sqlite"
        assert p.max_chunk_bytes == 100_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 10000

    async def test_profile_custom_max(self):
        a = _make_adapter(max_chunk_bytes=5000)
        assert a.profile().max_chunk_bytes == 5000

    async def test_write_chunk(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor())
        db.commit = AsyncMock()
        a._db = db

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "sqlite"
        assert len(ref.location) == 32  # uuid hex
        assert db.execute.await_count == 1
        assert db.commit.await_count == 1

        call_args = db.execute.call_args[0]
        assert "INSERT INTO" in call_args[0]
        params = call_args[1]
        assert params[1] == "m1"  # manifest_id
        assert params[4] == enc  # data

    async def test_read_chunk(self):
        a = _make_adapter()
        db = AsyncMock()
        enc = _sample_encoded()
        db.execute = AsyncMock(return_value=_mock_cursor(fetchone=(enc,)))
        a._db = db

        ref = StoredChunkRef(adapter_name="sqlite", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        call_args = db.execute.call_args[0]
        assert "SELECT data" in call_args[0]
        assert call_args[1] == ("abc123",)

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(fetchone=None))
        a._db = db

        ref = StoredChunkRef(adapter_name="sqlite", location="missing")
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(rowcount=1))
        db.commit = AsyncMock()
        a._db = db

        ref = StoredChunkRef(adapter_name="sqlite", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(rowcount=0))
        db.commit = AsyncMock()
        a._db = db

        ref = StoredChunkRef(adapter_name="sqlite", location="missing")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=_mock_cursor(fetchall=[("id1",), ("id2",), ("id3",)])
        )
        a._db = db

        refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "sqlite" for r in refs)
        assert [r.location for r in refs] == ["id1", "id2", "id3"]

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(fetchall=[]))
        a._db = db

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(fetchone=(1,)))
        a._db = db

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("connection lost"))
        a._db = db

        assert await a.health_check() is False

    async def test_custom_table_name(self):
        a = _make_adapter(table="my_chunks")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_mock_cursor(fetchall=[("x",)]))
        a._db = db

        await a.list_chunks("m1")

        call_args = db.execute.call_args[0]
        assert "my_chunks" in call_args[0]
