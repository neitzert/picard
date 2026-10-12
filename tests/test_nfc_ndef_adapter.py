"""Tests for the NFC/NDEF adapter -- mocked, no hardware required."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.nfc_ndef import NfcNdefAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"nfc-test",
    ).encode()


def _make_adapter(**kwargs) -> NfcNdefAdapter:
    return NfcNdefAdapter(**kwargs)


@pytest.mark.asyncio
class TestNfcNdefAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "nfc-ndef"
        assert p.max_chunk_bytes == 800
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 10

    async def test_write_chunk(self):
        a = _make_adapter()
        a._write_tag = AsyncMock(return_value=True)

        ref = await a.write_chunk(_sample_encoded())

        assert ref.adapter_name == "nfc-ndef"
        assert len(ref.location) == 16
        a._write_tag.assert_awaited_once()
        records = a._write_tag.call_args[0][0]
        assert len(records) == 1
        assert records[0]["type"] == "picard.chunk"
        assert records[0]["id"] == ref.location

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._read_tag = AsyncMock(return_value=[
            {"tnf": 0x04, "type": "picard.chunk", "id": "abc123", "payload": enc},
        ])

        ref = StoredChunkRef(adapter_name="nfc-ndef", location="abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._read_tag = AsyncMock(return_value=[
            {"tnf": 0x04, "type": "picard.chunk", "id": "other", "payload": "data"},
        ])

        ref = StoredChunkRef(adapter_name="nfc-ndef", location="missing")
        with pytest.raises(ValueError, match="not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._format_tag = AsyncMock(return_value=True)

        ref = StoredChunkRef(adapter_name="nfc-ndef", location="abc123")
        assert await a.delete_chunk(ref) is True
        a._format_tag.assert_awaited_once()

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._format_tag = AsyncMock(side_effect=Exception("tag not present"))

        ref = StoredChunkRef(adapter_name="nfc-ndef", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc = _sample_encoded("m1")
        a._read_tag = AsyncMock(return_value=[
            {"tnf": 0x04, "type": "picard.chunk", "id": "id1", "payload": enc},
            {"tnf": 0x04, "type": "picard.chunk", "id": "id2", "payload": "unrelated"},
            {"tnf": 0x01, "type": "text/plain", "id": "id3", "payload": "not-a-chunk"},
        ])

        refs = await a.list_chunks("m1")
        assert len(refs) == 1
        assert refs[0].location == "id1"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._read_tag = AsyncMock(return_value=[])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_read_failure(self):
        a = _make_adapter()
        a._read_tag = AsyncMock(side_effect=Exception("no tag"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._detect_reader = AsyncMock(return_value=True)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._detect_reader = AsyncMock(side_effect=Exception("no reader"))
        assert await a.health_check() is False

    async def test_custom_max_chunk_bytes(self):
        a = _make_adapter(max_chunk_bytes=400)
        assert a.profile().max_chunk_bytes == 400
