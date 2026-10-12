"""Tests for the DynamoDB adapter -- mocked, no AWS calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.dynamodb import DynamoDbAdapter, _pack_location, _unpack_location
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


def _make_adapter(**kwargs) -> DynamoDbAdapter:
    defaults = dict(table_name="picard-chunks", region="us-east-1")
    defaults.update(kwargs)
    return DynamoDbAdapter(**defaults)


class TestLocationPacking:
    def test_pack(self):
        loc = _pack_location("PCHK#m1", "b0#c0")
        assert loc == "PCHK#m1|b0#c0"

    def test_unpack(self):
        pk, sk = _unpack_location("PCHK#m1|b0#c0")
        assert pk == "PCHK#m1"
        assert sk == "b0#c0"

    def test_round_trip(self):
        loc = _pack_location("PCHK#m1", "b2#c3")
        assert _unpack_location(loc) == ("PCHK#m1", "b2#c3")


@pytest.mark.asyncio
class TestDynamoDbAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "dynamodb"
        assert p.max_chunk_bytes == 400_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 2000

    async def test_write_chunk(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "dynamodb"
        pk, sk = _unpack_location(ref.location)
        assert pk == "PCHK#m1"
        assert sk == "b0#c0"
        table.put_item.assert_called_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table
        enc = _sample_encoded()

        table.get_item.return_value = {"Item": {"data": enc}}

        ref = StoredChunkRef(
            adapter_name="dynamodb",
            location=_pack_location("PCHK#m1", "b0#c0"),
        )
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table

        table.get_item.return_value = {}

        ref = StoredChunkRef(
            adapter_name="dynamodb",
            location=_pack_location("PCHK#m1", "b0#c0"),
        )
        with pytest.raises(ValueError, match="chunk not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table

        ref = StoredChunkRef(
            adapter_name="dynamodb",
            location=_pack_location("PCHK#m1", "b0#c0"),
        )
        assert await a.delete_chunk(ref) is True
        table.delete_item.assert_called_once()

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table
        table.delete_item.side_effect = Exception("throttled")

        ref = StoredChunkRef(
            adapter_name="dynamodb",
            location=_pack_location("PCHK#m1", "b0#c0"),
        )
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table

        table.query.return_value = {
            "Items": [
                {"pk": "PCHK#m1", "sk": "b0#c0", "data": "..."},
                {"pk": "PCHK#m1", "sk": "b0#c1", "data": "..."},
            ]
        }

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "dynamodb" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table

        table.query.return_value = {"Items": []}
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table
        table.table_status = "ACTIVE"

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        table = MagicMock()
        a._table = table
        type(table).table_status = property(
            lambda self: (_ for _ in ()).throw(Exception("not found"))
        )

        assert await a.health_check() is False
