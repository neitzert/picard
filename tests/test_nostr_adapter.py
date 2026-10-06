"""Tests for the Nostr adapter — mocked, no network calls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.nostr import NostrAdapter, _compute_event_id
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


FAKE_PUBKEY = "ab" * 32
FAKE_SIG = "cd" * 64


def _make_adapter(**kwargs) -> NostrAdapter:
    defaults = dict(
        relay_url="wss://relay.example.com",
        private_key_hex="aa" * 32,
    )
    defaults.update(kwargs)
    a = NostrAdapter(**defaults)
    a._derive_pubkey = MagicMock(return_value=FAKE_PUBKEY)
    a._sign_event = MagicMock(return_value=FAKE_SIG)
    return a


def _mock_ws(messages: list[str | list]):
    """Return an async context manager that yields a mock websocket."""
    ws = AsyncMock()
    responses = [json.dumps(m) if isinstance(m, list) else m for m in messages]
    ws.recv = AsyncMock(side_effect=responses)
    ws.send = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ws)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, ws


class TestComputeEventId:
    def test_deterministic(self):
        event = {
            "pubkey": FAKE_PUBKEY,
            "created_at": 1700000000,
            "kind": 1,
            "tags": [],
            "content": "hello",
        }
        id1 = _compute_event_id(event)
        id2 = _compute_event_id(event)
        assert id1 == id2
        assert len(id1) == 64

    def test_different_content(self):
        base = {
            "pubkey": FAKE_PUBKEY,
            "created_at": 1700000000,
            "kind": 1,
            "tags": [],
        }
        id1 = _compute_event_id({**base, "content": "aaa"})
        id2 = _compute_event_id({**base, "content": "bbb"})
        assert id1 != id2


@pytest.mark.asyncio
class TestNostrAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "nostr"
        assert p.max_chunk_bytes == 8000
        assert p.persistent is True
        assert p.ordered is False

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        event_id = "ef" * 32

        ctx, ws = _mock_ws([["OK", event_id, True, ""]])
        a._connect = MagicMock(return_value=ctx)

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "nostr"
        assert ref.location == event_id
        sent = json.loads(ws.send.call_args_list[0].args[0])
        assert sent[0] == "EVENT"
        assert sent[1]["content"] == enc
        assert ["picard", "m1"] in sent[1]["tags"]

    async def test_write_chunk_rejected(self):
        a = _make_adapter()
        enc = _sample_encoded()

        ctx, ws = _mock_ws([["OK", "x", False, "blocked"]])
        a._connect = MagicMock(return_value=ctx)

        with pytest.raises(RuntimeError, match="relay rejected"):
            await a.write_chunk(enc)

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        event_id = "ef" * 32

        ctx, ws = _mock_ws([
            ["EVENT", "picard-read", {"id": event_id, "content": enc}],
        ])
        a._connect = MagicMock(return_value=ctx)

        ref = StoredChunkRef(adapter_name="nostr", location=event_id)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([["EOSE", "picard-read"]])
        a._connect = MagicMock(return_value=ctx)

        ref = StoredChunkRef(adapter_name="nostr", location="dead" * 16)
        with pytest.raises(ValueError, match="event not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        event_id = "ef" * 32

        ctx, ws = _mock_ws([["OK", event_id, True, ""]])
        a._connect = MagicMock(return_value=ctx)

        ref = StoredChunkRef(adapter_name="nostr", location=event_id)
        assert await a.delete_chunk(ref) is True

        sent = json.loads(ws.send.call_args_list[0].args[0])
        assert sent[0] == "EVENT"
        assert sent[1]["kind"] == 5
        assert ["e", event_id] in sent[1]["tags"]

    async def test_delete_chunk_failure(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([["OK", "x", False, "denied"]])
        a._connect = MagicMock(return_value=ctx)

        ref = StoredChunkRef(adapter_name="nostr", location="ab" * 32)
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._connect = MagicMock(side_effect=Exception("offline"))

        ref = StoredChunkRef(adapter_name="nostr", location="ab" * 32)
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([
            ["EVENT", "picard-list", {"id": "evt1", "content": "..."}],
            ["EVENT", "picard-list", {"id": "evt2", "content": "..."}],
            ["EOSE", "picard-list"],
        ])
        a._connect = MagicMock(return_value=ctx)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert refs[0].location == "evt1"
        assert refs[1].location == "evt2"

    async def test_list_chunks_empty(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([["EOSE", "picard-list"]])
        a._connect = MagicMock(return_value=ctx)

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([["EOSE", "ping"]])
        a._connect = MagicMock(return_value=ctx)

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        ctx, ws = _mock_ws([["NOTICE", "error"]])
        a._connect = MagicMock(return_value=ctx)

        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._connect = MagicMock(side_effect=Exception("unreachable"))

        assert await a.health_check() is False
