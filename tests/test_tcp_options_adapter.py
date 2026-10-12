"""Tests for the TCP options adapter — mocked, no network."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.tcp_options import (
    TcpOptionsAdapter,
    _OP_DELETE,
    _OP_LIST,
    _OP_PING,
    _OP_READ,
    _OP_WRITE,
    _STATUS_OK,
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


def _make_adapter(**kwargs) -> TcpOptionsAdapter:
    defaults = dict(host="127.0.0.1", port=9998)
    defaults.update(kwargs)
    return TcpOptionsAdapter(**defaults)


def _ok_response(payload: bytes = b"") -> bytes:
    body = struct.pack("B", _STATUS_OK) + payload
    return struct.pack(">I", len(body)) + body


def _err_response(payload: bytes = b"") -> bytes:
    body = struct.pack("B", 0x01) + payload
    return struct.pack(">I", len(body)) + body


def _mock_reader(data: bytes) -> AsyncMock:
    reader = AsyncMock()
    remaining = bytearray(data)

    async def readexactly(n: int) -> bytes:
        result = bytes(remaining[:n])
        del remaining[:n]
        return result

    reader.readexactly = readexactly
    return reader


def _mock_writer() -> MagicMock:
    writer = MagicMock()
    writer.write = MagicMock()
    writer.drain = AsyncMock()
    return writer


class TestTcpOptionsAdapter:
    @pytest.mark.asyncio
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "tcp-options"
        assert p.max_chunk_bytes == 1200
        assert p.persistent is False
        assert p.ordered is True

    @pytest.mark.asyncio
    async def test_write_chunk(self):
        a = _make_adapter()
        a._reader = _mock_reader(_ok_response())
        a._writer = _mock_writer()

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "tcp-options"
        assert len(ref.location) == 32  # 16 bytes hex
        a._writer.write.assert_called_once()
        frame = a._writer.write.call_args[0][0]
        assert frame[4] == _OP_WRITE

    @pytest.mark.asyncio
    async def test_write_chunk_failure(self):
        a = _make_adapter()
        a._reader = _mock_reader(_err_response())
        a._writer = _mock_writer()

        with pytest.raises(RuntimeError, match="write failed"):
            await a.write_chunk(_sample_encoded())

    @pytest.mark.asyncio
    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._reader = _mock_reader(_ok_response(enc.encode("utf-8")))
        a._writer = _mock_writer()

        ref = StoredChunkRef(adapter_name="tcp-options", location="ab" * 16)
        result = await a.read_chunk(ref)
        assert result == enc

        frame = a._writer.write.call_args[0][0]
        assert frame[4] == _OP_READ

    @pytest.mark.asyncio
    async def test_read_chunk_failure(self):
        a = _make_adapter()
        a._reader = _mock_reader(_err_response())
        a._writer = _mock_writer()

        ref = StoredChunkRef(adapter_name="tcp-options", location="ab" * 16)
        with pytest.raises(RuntimeError, match="read failed"):
            await a.read_chunk(ref)

    @pytest.mark.asyncio
    async def test_delete_chunk(self):
        a = _make_adapter()
        a._reader = _mock_reader(_ok_response())
        a._writer = _mock_writer()

        ref = StoredChunkRef(adapter_name="tcp-options", location="cd" * 16)
        assert await a.delete_chunk(ref) is True

        frame = a._writer.write.call_args[0][0]
        assert frame[4] == _OP_DELETE

    @pytest.mark.asyncio
    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        a._reader = _mock_reader(_err_response())
        a._writer = _mock_writer()

        ref = StoredChunkRef(adapter_name="tcp-options", location="cd" * 16)
        assert await a.delete_chunk(ref) is False

    @pytest.mark.asyncio
    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        a._writer = MagicMock()
        a._writer.write = MagicMock(side_effect=Exception("broken pipe"))
        a._writer.drain = AsyncMock()
        a._reader = AsyncMock()

        ref = StoredChunkRef(adapter_name="tcp-options", location="cd" * 16)
        assert await a.delete_chunk(ref) is False

    @pytest.mark.asyncio
    async def test_list_chunks(self):
        a = _make_adapter()
        ids = "aa" * 16 + "\n" + "bb" * 16
        a._reader = _mock_reader(_ok_response(ids.encode("utf-8")))
        a._writer = _mock_writer()

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert refs[0].location == "aa" * 16
        assert refs[1].location == "bb" * 16

        frame = a._writer.write.call_args[0][0]
        assert frame[4] == _OP_LIST

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        a = _make_adapter()
        a._reader = _mock_reader(_ok_response(b""))
        a._writer = _mock_writer()

        refs = await a.list_chunks("m1")
        assert refs == []

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        a = _make_adapter()
        a._reader = _mock_reader(_ok_response())
        a._writer = _mock_writer()

        assert await a.health_check() is True
        frame = a._writer.write.call_args[0][0]
        assert frame[4] == _OP_PING

    @pytest.mark.asyncio
    async def test_health_check_fail(self):
        a = _make_adapter()
        a._reader = _mock_reader(_err_response())
        a._writer = _mock_writer()

        assert await a.health_check() is False

    @pytest.mark.asyncio
    async def test_health_check_exception(self):
        a = _make_adapter()
        a._writer = MagicMock()
        a._writer.write = MagicMock(side_effect=ConnectionRefusedError())
        a._writer.drain = AsyncMock()
        a._reader = AsyncMock()

        assert await a.health_check() is False
        assert a._reader is None
        assert a._writer is None

    @pytest.mark.asyncio
    async def test_ensure_connection(self):
        a = _make_adapter()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()
        with patch("asyncio.open_connection", return_value=(mock_reader, mock_writer)) as mock_open:
            await a._ensure_connection()
            mock_open.assert_awaited_once_with("127.0.0.1", 9998)
            assert a._reader is mock_reader
            assert a._writer is mock_writer
