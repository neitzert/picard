"""Tests for the WebSocket adapter — mocked, no network calls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.websocket import WebSocketAdapter
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


def _make_adapter(**kwargs) -> WebSocketAdapter:
    defaults = dict(url="wss://store.example.com/ws")
    defaults.update(kwargs)
    return WebSocketAdapter(**defaults)


def _mock_ws(response: dict) -> AsyncMock:
    ws = AsyncMock()
    ws.recv.return_value = json.dumps(response)
    return ws


@pytest.mark.asyncio
class TestWebSocketAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "websocket"
        assert p.max_chunk_bytes == 60_000
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": True, "id": "test123"})
        a._ws = ws
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "websocket"
        assert len(ref.location) == 32  # uuid4 hex
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "write"
        assert sent["data"] == enc

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": False, "error": "server full"})
        a._ws = ws

        with pytest.raises(RuntimeError, match="write failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ws = _mock_ws({"ok": True, "data": enc})
        a._ws = ws

        ref = StoredChunkRef(adapter_name="websocket", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "read"
        assert sent["id"] == "abc123"

    async def test_read_chunk_failure(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": False, "error": "not found"})
        a._ws = ws

        ref = StoredChunkRef(adapter_name="websocket", location="missing")
        with pytest.raises(RuntimeError, match="read failed"):
            await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": True})
        a._ws = ws

        ref = StoredChunkRef(adapter_name="websocket", location="abc123")
        assert await a.delete_chunk(ref) is True
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "delete"

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": False})
        a._ws = ws

        ref = StoredChunkRef(adapter_name="websocket", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        ws = AsyncMock()
        ws.send.side_effect = Exception("connection lost")
        a._ws = ws

        ref = StoredChunkRef(adapter_name="websocket", location="abc123")
        assert await a.delete_chunk(ref) is False
        assert a._ws is None  # connection cleared on error

    async def test_list_chunks(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": True, "ids": ["id1", "id2", "id3"]})
        a._ws = ws

        refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "websocket" for r in refs)
        assert [r.location for r in refs] == ["id1", "id2", "id3"]
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "list"
        assert sent["manifest"] == "m1"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": True, "ids": []})
        a._ws = ws

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_failure(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": False})
        a._ws = ws

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        ws = _mock_ws({"ok": True})
        a._ws = ws

        assert await a.health_check() is True
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "ping"

    async def test_health_check_fail(self):
        a = _make_adapter()
        ws = AsyncMock()
        ws.send.side_effect = Exception("refused")
        a._ws = ws

        assert await a.health_check() is False
