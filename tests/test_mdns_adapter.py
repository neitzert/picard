"""Tests for the mDNS adapter -- mocked, no network calls."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.mdns import MdnsAdapter
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


def _make_adapter(**kwargs) -> MdnsAdapter:
    return MdnsAdapter(**kwargs)


def _build_txt_response(name: str, txt_data: str) -> bytes:
    """Build a minimal mDNS response with a TXT record."""
    name_bytes = MdnsAdapter._encode_dns_name(name)
    txt_bytes = MdnsAdapter._encode_txt(txt_data)
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    record = (
        name_bytes
        + struct.pack("!HHI", 16, 1, 120)
        + struct.pack("!H", len(txt_bytes))
        + txt_bytes
    )
    return header + record


def _build_ptr_response(service_type: str, instances: list[str]) -> bytes:
    """Build a minimal mDNS PTR response."""
    st_bytes = MdnsAdapter._encode_dns_name(service_type)
    header = struct.pack("!HHHHHH", 0, 0x8400, 1, len(instances), 0, 0)
    question = st_bytes + struct.pack("!HH", 12, 1)
    answers = b""
    for inst in instances:
        inst_bytes = MdnsAdapter._encode_dns_name(inst)
        answers += (
            st_bytes
            + struct.pack("!HHI", 12, 1, 120)
            + struct.pack("!H", len(inst_bytes))
            + inst_bytes
        )
    return header + question + answers


class TestMdnsHelpers:
    def test_encode_dns_name(self):
        result = MdnsAdapter._encode_dns_name("foo.bar.local.")
        assert result == b"\x03foo\x03bar\x05local\x00"

    def test_encode_txt(self):
        result = MdnsAdapter._encode_txt("hello")
        assert result == b"\x05hello"

    def test_instance_name(self):
        a = _make_adapter()
        assert a._instance_name("abc123") == "chunk-abc123._picard._tcp.local."


@pytest.mark.asyncio
class TestMdnsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "mdns"
        assert p.max_chunk_bytes == 200
        assert p.persistent is False
        assert p.ttl_seconds == 120

    async def test_write_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"\x00" * 12)
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "mdns"
        assert ref.location  # non-empty msg_id
        a._send_recv.assert_awaited_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        instance = a._instance_name("abc123")
        response = _build_txt_response(instance, enc)
        a._send_recv = AsyncMock(return_value=response)

        ref = StoredChunkRef(adapter_name="mdns", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc

    async def test_read_chunk_no_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"")

        ref = StoredChunkRef(adapter_name="mdns", location="abc123")
        with pytest.raises(ValueError, match="no mDNS response"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"\x00" * 12)

        ref = StoredChunkRef(adapter_name="mdns", location="abc123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("network error"))

        ref = StoredChunkRef(adapter_name="mdns", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"")

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"\x00" * 20)

        assert await a.health_check() is True

    async def test_health_check_no_response(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(return_value=b"")

        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        a._send_recv = AsyncMock(side_effect=Exception("timeout"))

        assert await a.health_check() is False
