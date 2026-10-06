"""Tests for the Gopher adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.gopher import GopherAdapter
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


def _make_adapter(**kwargs) -> GopherAdapter:
    defaults = dict(host="gopher.example.com", port=70)
    defaults.update(kwargs)
    return GopherAdapter(**defaults)


@pytest.mark.asyncio
class TestGopherAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "gopher"
        assert p.max_chunk_bytes == 50_000
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="OK")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "gopher"
        assert len(ref.location) == 16
        call_arg = a._send_recv.call_args[0][0]
        assert call_arg.startswith("PCHK-WRITE:")
        assert enc in call_arg

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=enc)

        ref = StoredChunkRef(adapter_name="gopher", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        a._send_recv.assert_awaited_once_with("PCHK-READ:abc123")

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="OK")

        ref = StoredChunkRef(adapter_name="gopher", location="abc123")
        assert await a.delete_chunk(ref) is True
        a._send_recv.assert_awaited_once_with("PCHK-DELETE:abc123")

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="NOT_FOUND")

        ref = StoredChunkRef(adapter_name="gopher", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("connection refused"))

        ref = StoredChunkRef(adapter_name="gopher", location="abc123")
        with pytest.raises(Exception):
            await a.delete_chunk(ref)

    async def test_list_chunks(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="id1\nid2\nid3\n")

        refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "gopher" for r in refs)
        locs = [r.location for r in refs]
        assert locs == ["id1", "id2", "id3"]
        a._send_recv.assert_awaited_once_with("PCHK-LIST:m1")

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_whitespace(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="  \n  \n")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="Welcome to Gopher")
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=ConnectionRefusedError)
        assert await a.health_check() is False

    async def test_health_check_timeout(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=TimeoutError)
        assert await a.health_check() is False
