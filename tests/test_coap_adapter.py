"""Tests for the CoAP adapter — mocked, no network calls."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.coap import CoapAdapter, _CLASS_SUCCESS, _CODE_GET, _CODE_PUT
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


def _make_adapter(**kwargs) -> CoapAdapter:
    defaults = dict(host="127.0.0.1", port=5683)
    defaults.update(kwargs)
    return CoapAdapter(**defaults)


def _build_response(code: int, payload: bytes = b"") -> bytes:
    ver_type_tkl = (1 << 6) | (2 << 4) | 4
    msg_id = 0
    token = b"\x00" * 4
    header = struct.pack("!BBH", ver_type_tkl, code, msg_id) + token
    if payload:
        return header + b"\xff" + payload
    return header


def _success_code(detail: int = 1) -> int:
    return (_CLASS_SUCCESS << 5) | detail


class TestCoapMessageBuilding:
    def test_resource_path(self):
        assert CoapAdapter._resource_path("m1", 0, 2) == "/picard/m1/b0/c2"

    def test_build_message_has_header(self):
        msg = CoapAdapter._build_coap_message(_CODE_GET, "/test", b"\xab\xcd")
        assert len(msg) >= 4
        assert (msg[0] >> 6) == 1
        assert msg[1] == _CODE_GET
        tkl = msg[0] & 0x0F
        assert tkl == 2

    def test_build_message_with_payload(self):
        payload = b"hello"
        msg = CoapAdapter._build_coap_message(
            _CODE_PUT, "/x", b"\x01", payload
        )
        assert b"\xff" + payload in msg

    def test_parse_response_success(self):
        resp = _build_response(_success_code(5), b"content-data")
        code, payload = CoapAdapter._parse_coap_response(resp)
        assert (code >> 5) == _CLASS_SUCCESS
        assert payload == b"content-data"

    def test_parse_response_no_payload(self):
        resp = _build_response(_success_code(2))
        code, payload = CoapAdapter._parse_coap_response(resp)
        assert (code >> 5) == _CLASS_SUCCESS
        assert payload == b""

    def test_parse_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            CoapAdapter._parse_coap_response(b"\x00\x00")


@pytest.mark.asyncio
class TestCoapAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "coap"
        assert p.max_chunk_bytes == 1000
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(1))
        )
        enc = _sample_encoded()
        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "coap"
        assert ref.location == "/picard/m1/b0/c0"
        a._send_recv.assert_awaited_once()

    async def test_write_chunk_failure(self):
        a = _make_adapter()
        bad_code = (4 << 5) | 4
        a._send_recv = AsyncMock(
            return_value=_build_response(bad_code)
        )
        with pytest.raises(RuntimeError, match="PUT failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(5), enc.encode())
        )
        ref = StoredChunkRef(adapter_name="coap", location="/picard/m1/b0/c0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(2))
        )
        ref = StoredChunkRef(adapter_name="coap", location="/picard/m1/b0/c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        bad_code = (4 << 5) | 4
        a._send_recv = AsyncMock(
            return_value=_build_response(bad_code)
        )
        ref = StoredChunkRef(adapter_name="coap", location="/picard/m1/b0/c0")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=OSError("unreachable"))
        ref = StoredChunkRef(adapter_name="coap", location="/picard/m1/b0/c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        body = "/picard/m1/b0/c0\n/picard/m1/b0/c1\n"
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(5), body.encode())
        )
        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "coap" for r in refs)
        locs = [r.location for r in refs]
        assert "/picard/m1/b0/c0" in locs
        assert "/picard/m1/b0/c1" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(5), b"")
        )
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=OSError("timeout"))
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(
            return_value=_build_response(_success_code(5))
        )
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=OSError("connection refused"))
        assert await a.health_check() is False
