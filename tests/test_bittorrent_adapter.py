"""Tests for the BitTorrent DHT adapter -- mocked, no network calls."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.bittorrent import (
    BitTorrentAdapter,
    _bdecode,
    _bencode,
)
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


def _make_adapter(**kwargs) -> BitTorrentAdapter:
    defaults = dict(
        bootstrap_nodes=[("127.0.0.1", 6881)],
        port=6881,
    )
    defaults.update(kwargs)
    return BitTorrentAdapter(**defaults)


class TestBencode:
    def test_int(self):
        assert _bencode(42) == b"i42e"

    def test_bytes(self):
        assert _bencode(b"spam") == b"4:spam"

    def test_string(self):
        assert _bencode("hello") == b"5:hello"

    def test_list(self):
        assert _bencode([1, b"two"]) == b"li1e3:twoe"

    def test_dict(self):
        encoded = _bencode({b"a": 1, b"b": 2})
        assert encoded == b"d1:ai1e1:bi2ee"

    def test_round_trip(self):
        original = {b"id": b"\x01\x02", b"v": b"data"}
        assert _bdecode(_bencode(original)) == original


@pytest.mark.asyncio
class TestBitTorrentAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "bittorrent-dht"
        assert p.max_chunk_bytes == 900
        assert p.persistent is True
        assert p.ordered is False
        assert p.ttl_seconds == 7200

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        bencoded = _bencode(enc.encode())
        expected_target = hashlib.sha1(bencoded).hexdigest()

        a._send_recv = AsyncMock(return_value=_bencode({
            b"t": b"\x00\x01",
            b"y": b"r",
            b"r": {b"id": b"\x00" * 20},
        }))

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "bittorrent-dht"
        assert ref.location == expected_target
        a._send_recv.assert_awaited_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        bencoded_value = _bencode(enc.encode())
        target = hashlib.sha1(bencoded_value).hexdigest()

        a._send_recv = AsyncMock(return_value=_bencode({
            b"t": b"\x00\x01",
            b"y": b"r",
            b"r": {
                b"id": b"\x00" * 20,
                b"v": enc.encode(),
            },
        }))

        ref = StoredChunkRef(adapter_name="bittorrent-dht", location=target)
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_all_nodes_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=ConnectionError("timeout"))

        ref = StoredChunkRef(adapter_name="bittorrent-dht", location="ab" * 20)
        with pytest.raises(ConnectionError):
            await a.read_chunk(ref)

    async def test_delete_chunk_noop(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="bittorrent-dht", location="ab" * 20)
        assert await a.delete_chunk(ref) is True

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_bencode({
            b"t": b"\x00\x01",
            b"y": b"r",
            b"r": {b"id": b"\x00" * 20},
        }))
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("unreachable"))
        assert await a.health_check() is False
