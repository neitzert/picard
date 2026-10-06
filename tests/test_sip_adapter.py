"""Tests for the SIP adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.sip import SipAdapter
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


def _make_adapter(**kwargs) -> SipAdapter:
    defaults = dict(
        server_host="127.0.0.1",
        server_port=5060,
        from_uri="sip:picard@example.com",
        to_uri="sip:store@example.com",
    )
    defaults.update(kwargs)
    return SipAdapter(**defaults)


def _ok_response(body: str = "") -> str:
    resp = "SIP/2.0 200 OK\r\nContent-Length: {}\r\n\r\n{}".format(
        len(body.encode()), body
    )
    return resp


def _error_response(code: int = 500) -> str:
    return f"SIP/2.0 {code} Error\r\nContent-Length: 0\r\n\r\n"


class TestSipAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "sip"
        assert p.max_chunk_bytes == 1200
        assert p.persistent is False
        assert p.ordered is False
        assert p.rate_limit_per_min == 30
        assert p.ttl_seconds == 3600

    def test_build_message(self):
        a = _make_adapter()
        msg = a._build_message("MESSAGE", {"X-PCHK-Op": "WRITE"}, "hello")
        assert msg.startswith("MESSAGE sip:store@example.com SIP/2.0\r\n")
        assert "X-PCHK-Op: WRITE" in msg
        assert msg.endswith("\r\nhello")
        assert "Content-Length: 5" in msg

    def test_parse_response(self):
        code, body = SipAdapter._parse_response(
            "SIP/2.0 200 OK\r\nContent-Length: 4\r\n\r\ntest"
        )
        assert code == 200
        assert body == "test"

    def test_parse_response_no_body(self):
        code, body = SipAdapter._parse_response(
            "SIP/2.0 404 Not Found\r\n\r\n"
        )
        assert code == 404
        assert body == ""


@pytest.mark.asyncio
class TestSipAdapterAsync:
    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "sip"
        assert len(ref.location) == 16
        a._send_recv.assert_awaited_once()
        call_arg = a._send_recv.call_args[0][0]
        assert "X-PCHK-Op: WRITE" in call_arg
        assert enc in call_arg

    async def test_write_chunk_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_response(500))

        with pytest.raises(RuntimeError, match="SIP write failed"):
            await a.write_chunk(_sample_encoded())

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=_ok_response(enc))

        ref = StoredChunkRef(adapter_name="sip", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        call_arg = a._send_recv.call_args[0][0]
        assert "X-PCHK-Op: READ" in call_arg
        assert "X-PCHK-Id: abc123" in call_arg

    async def test_read_chunk_error(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_response(404))

        ref = StoredChunkRef(adapter_name="sip", location="abc123")
        with pytest.raises(RuntimeError, match="SIP read failed"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())

        ref = StoredChunkRef(adapter_name="sip", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_error_response(404))

        ref = StoredChunkRef(adapter_name="sip", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        ref = StoredChunkRef(adapter_name="sip", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response("id1\nid2\nid3"))

        refs = await a.list_chunks("m1")

        assert len(refs) == 3
        assert all(r.adapter_name == "sip" for r in refs)
        locs = [r.location for r in refs]
        assert locs == ["id1", "id2", "id3"]

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response(""))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=_ok_response())
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("unreachable"))
        assert await a.health_check() is False
