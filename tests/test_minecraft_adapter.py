"""Tests for the Minecraft RCON adapter."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, patch

import pytest

from picard.adapters.minecraft_adapter import MinecraftAdapter


@pytest.fixture
def adapter():
    return MinecraftAdapter(
        host="mc.example.com",
        rcon_password="secret",
        rcon_port=25575,
    )


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "minecraft"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 30

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 256


class TestRconPacket:
    def test_pack_format(self, adapter):
        packet = adapter._pack_rcon(1, 3, "password")
        length = struct.unpack("<I", packet[:4])[0]
        assert length == 4 + 4 + len(b"password\x00\x00")

    def test_unpack_roundtrip(self, adapter):
        packet = adapter._pack_rcon(42, 2, "hello")
        # _unpack_rcon expects data WITHOUT the initial length prefix
        # packet is: length(4) + request_id(4) + type(4) + payload + \x00\x00
        req_id, msg_type, payload = adapter._unpack_rcon(packet)
        assert req_id == 42
        assert msg_type == 2
        assert payload == "hello"


def _mock_rcon_connection(adapter, responses):
    """Create a mock asyncio.open_connection that returns RCON responses."""
    resp_idx = [0]

    async def _open(*args, **kwargs):
        data = responses[resp_idx[0] % len(responses)]
        resp_idx[0] += 1
        length_bytes = data[:4]
        body = data[4:]

        reader = AsyncMock()
        reader.readexactly = AsyncMock(side_effect=[length_bytes, body])

        writer = AsyncMock()
        writer.write = lambda x: None
        writer.drain = AsyncMock()
        writer.close = lambda: None
        writer.wait_closed = AsyncMock()
        return reader, writer

    return _open


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.minecraft_adapter.asyncio.open_connection")
    async def test_round_trip(self, mock_conn, adapter):
        auth_resp = adapter._pack_rcon(1, 2, "")
        cmd_resp = adapter._pack_rcon(2, 0, "ok")
        responses = [auth_resp, cmd_resp] * 10
        mock_conn.side_effect = _mock_rcon_connection(adapter, responses)

        ref = await adapter.write_chunk("mc-data")
        assert ref.adapter_name == "minecraft"
        assert "mc://" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == "mc-data"

    async def test_read_missing(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="minecraft", location="mc://missing")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.minecraft_adapter.asyncio.open_connection")
    async def test_delete(self, mock_conn, adapter):
        response = adapter._pack_rcon(1, 2, "ok")
        mock_conn.side_effect = _mock_rcon_connection(adapter, [response] * 10)

        ref = await adapter.write_chunk("del-mc")
        assert await adapter.delete_chunk(ref) is True
        assert await adapter.delete_chunk(ref) is False


class TestCoords:
    def test_coords_for_chunk(self, adapter):
        x, y, z = adapter._coords_for_chunk(0)
        assert y == 64
        x2, y2, z2 = adapter._coords_for_chunk(1)
        assert x2 == x + 2


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0


@pytest.mark.asyncio
class TestList:
    @patch("picard.adapters.minecraft_adapter.asyncio.open_connection")
    async def test_list_chunks(self, mock_conn, adapter):
        response = adapter._pack_rcon(1, 2, "ok")
        mock_conn.side_effect = _mock_rcon_connection(adapter, [response] * 10)

        await adapter.write_chunk("PCHK:1:man1:0:0:1:ZGF0YTE=")
        refs = await adapter.list_chunks("man1")
        assert len(refs) >= 1


@pytest.mark.asyncio
class TestHealthCheck:
    @patch("picard.adapters.minecraft_adapter.asyncio.open_connection")
    async def test_healthy(self, mock_conn, adapter):
        response = adapter._pack_rcon(1, 2, "")
        mock_conn.side_effect = _mock_rcon_connection(adapter, [response])
        assert await adapter.health_check() is True

    @patch("picard.adapters.minecraft_adapter.asyncio.open_connection")
    async def test_unhealthy(self, mock_conn, adapter):
        mock_conn.side_effect = ConnectionRefusedError()
        assert await adapter.health_check() is False
