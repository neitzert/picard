"""Tests for the Matrix adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.matrix import MatrixAdapter
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


def _make_adapter(**kwargs) -> MatrixAdapter:
    defaults = dict(
        homeserver="https://matrix.example.com",
        access_token="syt_token_xxx",
        room_id="!room123:matrix.example.com",
    )
    defaults.update(kwargs)
    return MatrixAdapter(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
class TestMatrixAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "matrix"
        assert p.max_chunk_bytes == 60_000
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 60

    async def test_base_url(self):
        a = _make_adapter(homeserver="https://matrix.example.com/")
        assert a._base == "https://matrix.example.com/_matrix/client/v3"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.put.return_value = _mock_response({"event_id": "$evt123"})

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "matrix"
        assert ref.location == "$evt123"
        call_args = client.put.call_args
        assert "/send/m.room.message/" in call_args.args[0]
        body = call_args.kwargs["json"]
        assert body["msgtype"] == "m.text"
        assert body["body"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response({
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": enc},
        })

        ref = StoredChunkRef(adapter_name="matrix", location="$evt123")
        result = await a.read_chunk(ref)

        assert result == enc
        assert "/event/$evt123" in client.get.call_args.args[0]

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.put.return_value = _mock_response({"event_id": "$redact456"})

        ref = StoredChunkRef(adapter_name="matrix", location="$evt123")
        assert await a.delete_chunk(ref) is True
        assert "/redact/$evt123/" in client.put.call_args.args[0]

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.put.return_value = _mock_response({"errcode": "M_FORBIDDEN"})

        ref = StoredChunkRef(adapter_name="matrix", location="$evt123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.put.side_effect = Exception("connection refused")

        ref = StoredChunkRef(adapter_name="matrix", location="$evt123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc_m1 = _sample_encoded("m1")
        enc_m2 = _sample_encoded("m2")

        client.get.return_value = _mock_response({
            "chunk": [
                {"type": "m.room.message", "event_id": "$e1", "content": {"body": enc_m1}},
                {"type": "m.room.message", "event_id": "$e2", "content": {"body": enc_m2}},
                {"type": "m.room.message", "event_id": "$e3", "content": {"body": enc_m1}},
            ],
            "end": None,
        })

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "matrix" for r in refs)
        locs = [r.location for r in refs]
        assert "$e1" in locs
        assert "$e3" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"chunk": [], "end": None})

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded("m1")

        page1 = _mock_response({
            "chunk": [
                {"type": "m.room.message", "event_id": "$e1", "content": {"body": enc}},
            ],
            "end": "t2_token",
        })
        page2 = _mock_response({
            "chunk": [
                {"type": "m.room.message", "event_id": "$e2", "content": {"body": enc}},
            ],
            "end": None,
        })
        client.get.side_effect = [page1, page2]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert client.get.await_count == 2

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"user_id": "@bot:matrix.example.com"})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"errcode": "M_UNKNOWN_TOKEN"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False
