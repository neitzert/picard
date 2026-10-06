"""Tests for the Slack adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.slack import SlackAdapter
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


def _make_adapter(**kwargs) -> SlackAdapter:
    defaults = dict(
        bot_token="xoxb-test-token",
        channel_id="C0123456789",
    )
    defaults.update(kwargs)
    return SlackAdapter(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
class TestSlackAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "slack"
        assert p.max_chunk_bytes == 3900
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 60

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({
            "ok": True,
            "ts": "1234567890.123456",
            "channel": "C0123456789",
        })

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "slack"
        assert ref.location == "1234567890.123456"

        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["channel"] == "C0123456789"
        assert body["text"] == enc

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({
            "ok": False,
            "error": "channel_not_found",
        })

        with pytest.raises(RuntimeError, match="channel_not_found"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({
            "ok": True,
            "messages": [{"text": enc, "ts": "1234567890.123456"}],
        })

        ref = StoredChunkRef(adapter_name="slack", location="1234567890.123456")
        result = await a.read_chunk(ref)

        assert result == enc
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["channel"] == "C0123456789"
        assert body["latest"] == "1234567890.123456"
        assert body["oldest"] == "1234567890.123456"
        assert body["inclusive"] is True
        assert body["limit"] == 1

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": True})

        ref = StoredChunkRef(adapter_name="slack", location="1234567890.123456")
        assert await a.delete_chunk(ref) is True

        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["ts"] == "1234567890.123456"

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": False, "error": "message_not_found"})

        ref = StoredChunkRef(adapter_name="slack", location="1234567890.123456")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("network error")

        ref = StoredChunkRef(adapter_name="slack", location="1234567890.123456")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        client.post.return_value = _mock_response({
            "ok": True,
            "messages": [
                {"text": enc1, "ts": "1111.111"},
                {"text": enc2, "ts": "2222.222"},
                {"text": enc_other, "ts": "3333.333"},
                {"text": "just a normal message", "ts": "4444.444"},
            ],
            "response_metadata": {"next_cursor": ""},
        })

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "slack" for r in refs)
        locs = [r.location for r in refs]
        assert "1111.111" in locs
        assert "2222.222" in locs

    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)

        page1 = _mock_response({
            "ok": True,
            "messages": [{"text": enc1, "ts": "1111.111"}],
            "response_metadata": {"next_cursor": "cursor_abc"},
        })
        page2 = _mock_response({
            "ok": True,
            "messages": [{"text": enc2, "ts": "2222.222"}],
            "response_metadata": {"next_cursor": ""},
        })
        client.post.side_effect = [page1, page2]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert client.post.await_count == 2

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        })

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"ok": True})
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
