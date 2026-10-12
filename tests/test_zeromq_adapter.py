"""Tests for the ZeroMQ adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.zeromq_adapter import ZeromqAdapter


@pytest.fixture
def adapter():
    return ZeromqAdapter(endpoint="tcp://localhost:5555")


@pytest.fixture
def mock_zmq(monkeypatch):
    """Set up mock zmq module and socket."""
    mock_socket = AsyncMock()
    mock_socket.send_multipart = AsyncMock()
    mock_socket.recv = AsyncMock(return_value=b"OK")
    mock_socket.close = MagicMock()
    mock_socket.connect = MagicMock()
    mock_socket.setsockopt = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.socket = MagicMock(return_value=mock_socket)

    import sys
    mock_mod = MagicMock()
    mock_mod.REQ = 3
    mock_mod.RCVTIMEO = 27
    mock_mod.SNDTIMEO = 28
    mock_mod.asyncio.Context.instance = MagicMock(return_value=mock_ctx)
    sys.modules["zmq"] = mock_mod
    sys.modules["zmq.asyncio"] = mock_mod.asyncio

    yield {"socket": mock_socket, "ctx": mock_ctx, "mod": mock_mod}

    del sys.modules["zmq"]
    del sys.modules["zmq.asyncio"]


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "zeromq"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10000


@pytest.mark.asyncio
class TestWriteChunk:
    async def test_write_returns_ref(self, adapter, mock_zmq):
        ref = await adapter.write_chunk("PCHK:1:abc123:0:0:1:dGVzdA==")
        assert ref.adapter_name == "zeromq"
        assert "zmq://tcp://localhost:5555/" in ref.location
        mock_zmq["socket"].send_multipart.assert_called_once()

    async def test_write_opaque_data(self, adapter, mock_zmq):
        ref = await adapter.write_chunk("deadbeef0102030405")
        assert ref.adapter_name == "zeromq"
        assert ref.location.startswith("zmq://")

    async def test_write_fail_raises(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(return_value=b"ERROR")
        with pytest.raises(RuntimeError, match="ZMQ PUT failed"):
            await adapter.write_chunk("PCHK:1:abc123:0:0:1:dGVzdA==")


@pytest.mark.asyncio
class TestReadChunk:
    async def test_read_returns_data(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(return_value=b"PCHK:1:abc123:0:0:1:dGVzdA==")
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="zeromq",
            location="zmq://tcp://localhost:5555/abc123/0/0",
        )
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc123:0:0:1:dGVzdA=="


@pytest.mark.asyncio
class TestDeleteChunk:
    async def test_delete_ok(self, adapter, mock_zmq):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="zeromq",
            location="zmq://tcp://localhost:5555/abc123/0/0",
        )
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(return_value=b"NOTFOUND")
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="zeromq",
            location="zmq://tcp://localhost:5555/nope",
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_returns_refs(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(
            return_value=b"abc123/0/0\nabc123/0/1\n"
        )
        refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(return_value=b"PONG")
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter, mock_zmq):
        mock_zmq["socket"].recv = AsyncMock(side_effect=Exception("timeout"))
        assert await adapter.health_check() is False
