"""Tests for the Discord adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.discord import DiscordAdapter
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


def _make_adapter(**kwargs) -> DiscordAdapter:
    defaults = dict(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        channel_id="chan-456",
        bot_token="bot-tok-xxx",
    )
    defaults.update(kwargs)
    return DiscordAdapter(**defaults)


def _mock_response(json_data: dict | list, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestDiscordAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "discord"
        assert p.max_chunk_bytes == 1900
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 30

    async def test_messages_url(self):
        a = _make_adapter()
        assert a._messages_url() == "https://discord.com/api/v10/channels/chan-456/messages"
        assert a._messages_url("msg1") == "https://discord.com/api/v10/channels/chan-456/messages/msg1"

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        mock_resp = _mock_response({"id": "msg-789"})
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        import picard.adapters.discord as mod
        original_client = mod.httpx if hasattr(mod, "httpx") else None
        import httpx as _httpx
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("picard.adapters.discord.httpx", MagicMock(AsyncClient=MagicMock(return_value=mock_client)), raising=False)
            # Need to handle the import inside write_chunk
            import importlib
            # Simpler: just mock at module level
            pass

        # Use a more direct approach — mock httpx.AsyncClient as context manager
        mock_cm = AsyncMock()
        mock_cm.post.return_value = mock_resp
        mock_cm.__aenter__ = AsyncMock(return_value=mock_cm)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        from unittest.mock import patch
        with patch("httpx.AsyncClient", return_value=mock_cm):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "discord"
        assert ref.location == "msg-789"

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response({"content": enc})

        ref = StoredChunkRef(adapter_name="discord", location="msg-789")
        result = await a.read_chunk(ref)

        assert result == enc
        client.get.assert_awaited_once_with(
            "https://discord.com/api/v10/channels/chan-456/messages/msg-789"
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({}, status_code=204)

        ref = StoredChunkRef(adapter_name="discord", location="msg-789")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({}, status_code=404)

        ref = StoredChunkRef(adapter_name="discord", location="msg-789")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="discord", location="msg-789")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)
        enc_other = _sample_encoded("m2", 0, 0)

        client.get.return_value = _mock_response([
            {"id": "1", "content": enc1},
            {"id": "2", "content": enc2},
            {"id": "3", "content": enc_other},
        ])

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "discord" for r in refs)
        locs = {r.location for r in refs}
        assert locs == {"1", "2"}

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response([])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc1 = _sample_encoded("m1", 0, 0)
        enc2 = _sample_encoded("m1", 0, 1)

        page1 = [{"id": str(i), "content": enc1} for i in range(100)]
        page2 = [{"id": "200", "content": enc2}]

        client.get.side_effect = [
            _mock_response(page1),
            _mock_response(page2),
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 101
        assert client.get.await_count == 2

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"id": "chan-456"}, status_code=200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({}, status_code=401)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")

        assert await a.health_check() is False
