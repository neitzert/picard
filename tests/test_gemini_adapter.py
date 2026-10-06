"""Tests for the Gemini adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.gemini import GeminiAdapter
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


def _make_adapter(**kwargs) -> GeminiAdapter:
    defaults = dict(host="gemini.example.com", port=1965)
    defaults.update(kwargs)
    return GeminiAdapter(**defaults)


@pytest.mark.asyncio
class TestGeminiAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "gemini"
        assert p.max_chunk_bytes == 50_000
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(20, "text/plain", "OK"))
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "gemini"
        assert len(ref.location) == 16
        call_url = a._send_recv.call_args[0][0]
        assert "/picard/write/" in call_url
        assert "data=" in call_url

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=(20, "text/plain", enc))

        ref = StoredChunkRef(adapter_name="gemini", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        call_url = a._send_recv.call_args[0][0]
        assert call_url == "gemini://gemini.example.com/picard/read/abc123"

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(51, "Not found", ""))

        ref = StoredChunkRef(adapter_name="gemini", location="abc123")
        with pytest.raises(ValueError, match="status 51"):
            await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(20, "text/plain", "OK"))

        ref = StoredChunkRef(adapter_name="gemini", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(51, "Not found", ""))

        ref = StoredChunkRef(adapter_name="gemini", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("connection error"))

        ref = StoredChunkRef(adapter_name="gemini", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(20, "text/plain", "id1\nid2\nid3\n"))

        refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "gemini" for r in refs)
        locs = [r.location for r in refs]
        assert locs == ["id1", "id2", "id3"]

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(20, "text/plain", ""))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_not_found(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(51, "Not found", ""))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=(20, "text/gemini", "Welcome"))
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=ConnectionRefusedError)
        assert await a.health_check() is False

    async def test_health_check_timeout(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=TimeoutError)
        assert await a.health_check() is False
