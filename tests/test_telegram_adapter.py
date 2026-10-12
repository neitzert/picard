"""Tests for the Telegram adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.telegram import TelegramAdapter
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


def _make_adapter(**kwargs) -> TelegramAdapter:
    defaults = dict(bot_token="123:ABC", chat_id="-100999")
    defaults.update(kwargs)
    return TelegramAdapter(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
class TestTelegramAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "telegram"
        assert p.max_chunk_bytes == 4000
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 20

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({
            "ok": True,
            "result": {"message_id": 42},
        })

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "telegram"
        assert ref.location == "42"
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["chat_id"] == "-100999"
        assert body["text"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.side_effect = [
            _mock_response({
                "ok": True,
                "result": {"message_id": 99, "text": enc},
            }),
            _mock_response({"ok": True, "result": True}),
        ]

        ref = StoredChunkRef(adapter_name="telegram", location="42")
        result = await a.read_chunk(ref)

        assert result == enc
        assert client.post.await_count == 2
        fwd_call = client.post.call_args_list[0]
        assert "forwardMessage" in fwd_call.args[0]
        del_call = client.post.call_args_list[1]
        assert "deleteMessage" in del_call.args[0]

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": True, "result": True})

        ref = StoredChunkRef(adapter_name="telegram", location="42")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": False, "result": False})

        ref = StoredChunkRef(adapter_name="telegram", location="42")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("network error")

        ref = StoredChunkRef(adapter_name="telegram", location="42")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": True, "result": {"id": 123, "first_name": "Bot"}})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": False})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("timeout")
        assert await a.health_check() is False

    async def test_base_url(self):
        a = _make_adapter(bot_token="999:XYZ")
        assert a._base == "https://api.telegram.org/bot999:XYZ"
