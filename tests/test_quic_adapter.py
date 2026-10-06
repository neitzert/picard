"""Tests for the QUIC/HTTP3 adapter -- mocked httpx, no real server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.chunker import Chunk
from picard.adapters.quic_adapter import QuicAdapter


def _resp(code, text="", json_data=None):
    r = MagicMock()
    r.status_code = code
    r.text = text
    r.json.return_value = json_data or []
    r.raise_for_status = MagicMock()
    if code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {code}")
    return r


class _MockAsyncClient:
    def __init__(self, **kw):
        self._kw = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def put(self, url, **kw):
        return _resp(200)

    async def get(self, url, **kw):
        return _resp(200, text=kw.get("_text", ""))

    async def delete(self, url, **kw):
        return _resp(204)

    async def head(self, url, **kw):
        return _resp(200)


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> QuicAdapter:
    return QuicAdapter(url="https://quic.example.com/store", **kwargs)


@pytest.mark.asyncio
@patch("httpx.AsyncClient", _MockAsyncClient)
class TestQuicAdapter:
    async def test_profile(self):
        p = _make_adapter().profile()
        assert p.name == "quic"
        assert p.max_chunk_bytes == 65536
        assert p.persistent is True
        assert p.rate_limit_per_min == 1000

    async def test_profile_custom_max(self):
        assert _make_adapter(max_chunk_bytes=4096).profile().max_chunk_bytes == 4096

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "quic"
        assert "quic.example.com" in ref.location
        assert "m1" in ref.location

    async def test_write_opaque(self):
        a = _make_adapter()
        ref = await a.write_chunk("opaque-data")
        assert ref.adapter_name == "quic"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        original_get = _MockAsyncClient.get

        async def _get(self, url, **kw):
            return _resp(200, text=enc)

        _MockAsyncClient.get = _get
        try:
            ref = StoredChunkRef(
                adapter_name="quic",
                location="quic://quic.example.com/store/chunks/m1/0_0",
            )
            result = await a.read_chunk(ref)
            assert result == enc
        finally:
            _MockAsyncClient.get = original_get

    async def test_delete_chunk(self):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="quic",
            location="quic://quic.example.com/store/chunks/m1/0_0",
        )
        assert await a.delete_chunk(ref) is True

    async def test_delete_not_found(self):
        a = _make_adapter()
        original_delete = _MockAsyncClient.delete

        async def _del(self, url, **kw):
            return _resp(404)

        _MockAsyncClient.delete = _del
        try:
            ref = StoredChunkRef(
                adapter_name="quic",
                location="quic://quic.example.com/store/chunks/m1/0_0",
            )
            assert await a.delete_chunk(ref) is False
        finally:
            _MockAsyncClient.delete = original_delete

    async def test_list_chunks(self):
        a = _make_adapter()
        original_get = _MockAsyncClient.get

        async def _get(self, url, **kw):
            return _resp(200, json_data=["0_0", "0_1", "1_0"])

        _MockAsyncClient.get = _get
        try:
            refs = await a.list_chunks("m1")
            assert len(refs) == 3
            assert all(r.adapter_name == "quic" for r in refs)
        finally:
            _MockAsyncClient.get = original_get

    async def test_list_chunks_error(self):
        a = _make_adapter()
        original_get = _MockAsyncClient.get

        async def _get(self, url, **kw):
            raise ConnectionError("refused")

        _MockAsyncClient.get = _get
        try:
            assert await a.list_chunks("m1") == []
        finally:
            _MockAsyncClient.get = original_get

    async def test_health_check_ok(self):
        assert await _make_adapter().health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        original_head = _MockAsyncClient.head

        async def _head(self, url, **kw):
            raise ConnectionError("timeout")

        _MockAsyncClient.head = _head
        try:
            assert await a.health_check() is False
        finally:
            _MockAsyncClient.head = original_head

    async def test_capacity(self):
        cap = await _make_adapter().capacity()
        assert cap["used_bytes"] is None
        assert cap["chunk_count"] is None

    async def test_location_key_opaque(self):
        loc = _make_adapter()._location_key("opaque-no-pchk")
        assert loc.startswith("quic://quic.example.com")
