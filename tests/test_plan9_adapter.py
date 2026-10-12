"""Tests for the 9P/Plan 9 adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.plan9_adapter import Plan9Adapter


@pytest.fixture
def adapter():
    return Plan9Adapter("9p.example.com")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "9p"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWrite:
    async def test_write_returns_ref(self, adapter):
        import struct

        def make_9p_reply(msg_type, tag, body):
            size = 4 + 1 + 2 + len(body)
            return struct.pack("<IBH", size, msg_type, tag) + body

        # Build proper 9P responses
        rversion_body = struct.pack("<I", 73728) + struct.pack("<H", 6) + b"9P2000"
        rattach_body = b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        rwalk_body = struct.pack("<H", 0)
        rcreate_body = b"\x00" * 17
        rwrite_body = struct.pack("<I", 20)
        rclunk_body = b""

        responses = []
        for msg_type, tag, body in [
            (101, 0xFFFF, rversion_body),
            (105, 1, rattach_body),
            (111, 2, rwalk_body),
            (115, 3, rcreate_body),
            (119, 4, rwrite_body),
            (121, 5, rclunk_body),
        ]:
            full = make_9p_reply(msg_type, tag, body)
            responses.append(full[:7])   # header
            responses.append(full[7:])   # body

        mock_reader = AsyncMock()
        mock_reader.readexactly = AsyncMock(side_effect=responses)
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            ref = await adapter.write_chunk("PCHK:1:man1:0:0:1:aGVsbG8=")

        assert ref.adapter_name == "9p"
        assert "9p://9p.example.com/picard/" in ref.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_failure(self, adapter):
        ref = StoredChunkRef(
            adapter_name="9p",
            location="9p://9p.example.com/picard/test.0.0",
        )

        with patch("asyncio.open_connection", AsyncMock(side_effect=Exception)):
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        import struct

        def make_reply(msg_type, tag, body):
            size = 4 + 1 + 2 + len(body)
            return struct.pack("<IBH", size, msg_type, tag) + body

        rversion_body = struct.pack("<I", 73728) + struct.pack("<H", 6) + b"9P2000"
        rattach_body = b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"

        responses = []
        for msg_type, tag, body in [(101, 0xFFFF, rversion_body), (105, 1, rattach_body)]:
            full = make_reply(msg_type, tag, body)
            responses.append(full[:7])
            responses.append(full[7:])

        mock_reader = AsyncMock()
        mock_reader.readexactly = AsyncMock(side_effect=responses)
        mock_writer = AsyncMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("asyncio.open_connection", AsyncMock(side_effect=Exception)):
            assert await adapter.health_check() is False
