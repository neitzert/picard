"""Tests for the etcd adapter — mocked, no network calls."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.etcd import EtcdAdapter, _b64e, _b64d, _prefix_range_end
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


def _make_adapter(**kwargs) -> EtcdAdapter:
    defaults = dict(endpoint="http://localhost:2379")
    defaults.update(kwargs)
    return EtcdAdapter(**defaults)


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


class TestHelpers:
    def test_b64_roundtrip(self):
        assert _b64d(_b64e("hello/world")) == "hello/world"

    def test_prefix_range_end(self):
        end = _prefix_range_end("/picard/m1/")
        decoded = base64.b64decode(end)
        assert decoded == b"/picard/m1" + bytes([ord("/") + 1])


@pytest.mark.asyncio
class TestEtcdAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "etcd"
        assert p.max_chunk_bytes == 1_500_000
        assert p.persistent is True
        assert p.ttl_seconds is None

    async def test_profile_with_ttl(self):
        a = _make_adapter(ttl_seconds=300)
        assert a.profile().ttl_seconds == 300

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "/picard/m1/b0_c2"

    async def test_write_chunk_no_ttl(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({})

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "etcd"
        assert ref.location == "/picard/m1/b0_c0"
        client.post.assert_awaited_once()
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert "lease" not in body

    async def test_write_chunk_with_ttl(self):
        a = _make_adapter(ttl_seconds=600)
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.side_effect = [
            _mock_response({"ID": "12345"}),  # lease grant
            _mock_response({}),  # kv put
        ]

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "etcd"
        assert client.post.await_count == 2

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({
            "kvs": [{"key": _b64e("/picard/m1/b0_c0"), "value": _b64e(enc)}],
        })

        ref = StoredChunkRef(adapter_name="etcd", location="/picard/m1/b0_c0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"kvs": []})

        ref = StoredChunkRef(adapter_name="etcd", location="/picard/m1/b0_c99")
        with pytest.raises(ValueError, match="key not found"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"deleted": "1"})

        ref = StoredChunkRef(adapter_name="etcd", location="/picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"deleted": "0"})

        ref = StoredChunkRef(adapter_name="etcd", location="/picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="etcd", location="/picard/m1/b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({
            "kvs": [
                {"key": _b64e("/picard/m1/b0_c0"), "value": _b64e("data1")},
                {"key": _b64e("/picard/m1/b0_c1"), "value": _b64e("data2")},
            ],
        })

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "etcd" for r in refs)
        locs = [r.location for r in refs]
        assert "/picard/m1/b0_c0" in locs
        assert "/picard/m1/b0_c1" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response({"kvs": []})

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"health": "true"})
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"health": "false"})
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False
