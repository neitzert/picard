"""Tests for the SSDP adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ssdp import SsdpAdapter
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


def _make_adapter(**kwargs) -> SsdpAdapter:
    return SsdpAdapter(**kwargs)


def _ssdp_response(msg_id: str, encoded_chunk: str) -> str:
    return (
        "HTTP/1.1 200 OK\r\n"
        f"USN: uuid:{msg_id}::urn:picard:chunk:1\r\n"
        f"ST: uuid:{msg_id}::urn:picard:chunk:1\r\n"
        f"X-PCHK-DATA: {encoded_chunk}\r\n"
        "\r\n"
    )


class TestSsdpHelpers:
    def test_build_notify(self):
        msg = SsdpAdapter._build_notify("abc123", "test-data")
        assert "NOTIFY * HTTP/1.1" in msg
        assert "X-PCHK-DATA: test-data" in msg
        assert "uuid:abc123" in msg
        assert "ssdp:alive" in msg

    def test_build_byebye(self):
        msg = SsdpAdapter._build_byebye("abc123")
        assert "ssdp:byebye" in msg
        assert "uuid:abc123" in msg

    def test_build_msearch(self):
        msg = SsdpAdapter._build_msearch("ssdp:all")
        assert "M-SEARCH * HTTP/1.1" in msg
        assert "ST: ssdp:all" in msg

    def test_parse_header(self):
        resp = "HTTP/1.1 200 OK\r\nX-Foo: bar\r\nX-Baz: qux\r\n\r\n"
        assert SsdpAdapter._parse_header(resp, "X-Foo") == "bar"
        assert SsdpAdapter._parse_header(resp, "X-Baz") == "qux"
        assert SsdpAdapter._parse_header(resp, "X-Missing") is None

    def test_parse_usn_id(self):
        assert SsdpAdapter._parse_usn_id("uuid:abc::urn:picard:chunk:1") == "abc"
        assert SsdpAdapter._parse_usn_id("") is None


@pytest.mark.asyncio
class TestSsdpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ssdp"
        assert p.max_chunk_bytes == 800
        assert p.persistent is False
        assert p.ttl_seconds == 1800

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ssdp"
        assert len(ref.location) == 16
        a._send_recv.assert_awaited_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._send_recv = AsyncMock(return_value=_ssdp_response("abc123", enc))

        ref = StoredChunkRef(adapter_name="ssdp", location="abc123")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_no_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="")

        ref = StoredChunkRef(adapter_name="ssdp", location="abc123")
        with pytest.raises(ValueError, match="no SSDP response"):
            await a.read_chunk(ref)

    async def test_read_chunk_no_data_header(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(
            return_value="HTTP/1.1 200 OK\r\nUSN: uuid:abc\r\n\r\n"
        )

        ref = StoredChunkRef(adapter_name="ssdp", location="abc123")
        with pytest.raises(ValueError, match="no X-PCHK-DATA"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="")

        ref = StoredChunkRef(adapter_name="ssdp", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("network"))

        ref = StoredChunkRef(adapter_name="ssdp", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        enc = _sample_encoded(manifest_id="m1")
        responses = [
            _ssdp_response("id1", enc),
            _ssdp_response("id2", _sample_encoded(manifest_id="m2")),
        ]
        a._send_recv_all = AsyncMock(return_value=responses)

        refs = await a.list_chunks("m1")
        assert len(refs) == 1
        assert refs[0].location == "id1"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv_all = AsyncMock(return_value=[])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="HTTP/1.1 200 OK\r\n\r\n")
        assert await a.health_check() is True

    async def test_health_check_no_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value="")
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("err"))
        assert await a.health_check() is False
