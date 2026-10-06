"""Tests for the ICMP adapter -- mocked, no network or raw sockets."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.icmp import (
    IcmpAdapter,
    _CMD_DATA,
    _CMD_ERR,
    _CMD_FETCH,
    _CMD_INDEX,
    _CMD_LIST,
    _CMD_OK,
    _CMD_STORE,
    _build_picard_payload,
    _icmp_checksum,
    _parse_picard_payload,
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


def _make_adapter(**kwargs) -> IcmpAdapter:
    defaults = dict(target_host="192.168.1.100")
    defaults.update(kwargs)
    return IcmpAdapter(**defaults)


class TestWireProtocol:
    def test_checksum_zero_data(self):
        chk = _icmp_checksum(b"\x00\x00")
        assert chk == 0xFFFF

    def test_checksum_round_trip(self):
        data = b"PCHK\x01\x00\x01\x00\x03foo"
        chk = _icmp_checksum(data)
        assert isinstance(chk, int)
        assert 0 <= chk <= 0xFFFF

    def test_build_parse_payload_round_trip(self):
        payload = _build_picard_payload(_CMD_STORE, 42, "m1/0/0", b"hello")
        cmd, seq, key, data = _parse_picard_payload(payload)
        assert cmd == _CMD_STORE
        assert seq == 42
        assert key == "m1/0/0"
        assert data == b"hello"

    def test_build_parse_empty_data(self):
        payload = _build_picard_payload(_CMD_FETCH, 1, "k")
        cmd, seq, key, data = _parse_picard_payload(payload)
        assert cmd == _CMD_FETCH
        assert key == "k"
        assert data == b""

    def test_parse_bad_magic_raises(self):
        with pytest.raises(ValueError, match="not a PICARD"):
            _parse_picard_payload(b"BAAD\x01\x00\x01\x00\x01k")


@pytest.mark.asyncio
class TestIcmpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "icmp"
        assert p.max_chunk_bytes == 1400
        assert p.persistent is False
        assert p.ordered is False
        assert p.ttl_seconds == 3600

    async def test_key_format(self):
        key = IcmpAdapter._key("manifest1", 2, 3)
        assert key == "manifest1/2/3"

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch.object(a, "_send_recv", return_value=(_CMD_OK, "m1/0/0", b"")):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "icmp"
        assert ref.location == "icmp://192.168.1.100/m1/0/0"

    async def test_write_chunk_rejected(self):
        a = _make_adapter()
        enc = _sample_encoded()

        with patch.object(a, "_send_recv", return_value=(_CMD_ERR, "", b"")):
            with pytest.raises(IOError, match="rejected STORE"):
                await a.write_chunk(enc)

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        ref = StoredChunkRef(
            adapter_name="icmp",
            location="icmp://192.168.1.100/m1/0/0",
        )

        with patch.object(a, "_send_recv", return_value=(_CMD_DATA, "m1/0/0", enc.encode("utf-8"))):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="icmp",
            location="icmp://192.168.1.100/m1/0/0",
        )

        with patch.object(a, "_send_recv", return_value=(_CMD_ERR, "", b"")):
            with pytest.raises(KeyError, match="chunk not found"):
                await a.read_chunk(ref)

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="icmp",
            location="icmp://192.168.1.100/m1/0/0",
        )

        with patch.object(a, "_send_recv", return_value=(_CMD_OK, "", b"")):
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(
            adapter_name="icmp",
            location="icmp://192.168.1.100/m1/0/0",
        )

        with patch.object(a, "_send_recv", return_value=(_CMD_ERR, "", b"")):
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        keys = "m1/0/0\nm1/0/1\nm1/1/0"

        with patch.object(a, "_send_recv", return_value=(_CMD_LIST, "", keys.encode("utf-8"))):
            refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "icmp" for r in refs)
        assert refs[0].location == "icmp://192.168.1.100/m1/0/0"

    async def test_list_chunks_empty(self):
        a = _make_adapter()

        with patch.object(a, "_send_recv", return_value=(_CMD_ERR, "", b"")):
            refs = await a.list_chunks("m1")
            assert refs == []

    async def test_list_chunks_empty_data(self):
        a = _make_adapter()

        with patch.object(a, "_send_recv", return_value=(_CMD_LIST, "", b"")):
            refs = await a.list_chunks("m1")
            assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        mock_sock = MagicMock()
        with patch("picard.adapters.icmp.socket.socket", return_value=mock_sock):
            assert await a.health_check() is True
            mock_sock.close.assert_called_once()

    async def test_health_check_no_permission(self):
        a = _make_adapter()
        with patch("picard.adapters.icmp.socket.socket", side_effect=PermissionError):
            assert await a.health_check() is False

    async def test_seq_wraps(self):
        a = _make_adapter()
        a._seq = 0xFFFF
        assert a._next_seq() == 0
