"""Tests for the IRC adapter — mocked, no network calls."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.irc import IrcAdapter
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


def _make_adapter(**kwargs) -> IrcAdapter:
    defaults = dict(
        server="irc.example.com",
        nickname="picard-bot",
        channel="#chunks",
    )
    defaults.update(kwargs)
    return IrcAdapter(**defaults)


def _mock_writer() -> MagicMock:
    writer = MagicMock()
    writer.write = MagicMock()
    return writer


def _mock_reader(lines: list[str]) -> AsyncMock:
    reader = AsyncMock()
    encoded = [f"{l}\r\n".encode("utf-8") for l in lines]
    reader.readline = AsyncMock(side_effect=encoded)
    return reader


@pytest.mark.asyncio
class TestIrcAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "irc"
        assert p.max_chunk_bytes == 400
        assert p.persistent is False
        assert p.ordered is False
        assert p.rate_limit_per_min == 20

    async def test_channel_prefix(self):
        a = _make_adapter(channel="chunks")
        assert a._channel == "#chunks"
        a2 = _make_adapter(channel="#already")
        assert a2._channel == "#already"

    async def test_write_chunk(self):
        a = _make_adapter()
        writer = _mock_writer()
        a._writer = writer
        a._reader = AsyncMock()

        async def fake_connect():
            pass
        a._connect = fake_connect

        enc = _sample_encoded()
        with patch("picard.adapters.irc.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value = MagicMock(hex="aabbccddee11223344556677")
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "irc"
        assert ref.location == "aabbccddee11"
        written = writer.write.call_args[0][0].decode("utf-8")
        assert "PRIVMSG #chunks :PCHK-STR:aabbccddee11:" in written
        assert enc in written

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        msg_id = "abc123def456"

        reader = _mock_reader([
            f":bot!u@h PRIVMSG #chunks :PCHK-RSP:{msg_id}:{enc}",
        ])
        writer = _mock_writer()
        a._writer = writer
        a._reader = reader

        async def fake_connect():
            pass
        a._connect = fake_connect

        ref = StoredChunkRef(adapter_name="irc", location=msg_id)
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        writer = _mock_writer()
        a._writer = writer
        a._reader = AsyncMock()

        async def fake_connect():
            pass
        a._connect = fake_connect

        ref = StoredChunkRef(adapter_name="irc", location="abc123")
        assert await a.delete_chunk(ref) is True
        written = writer.write.call_args[0][0].decode("utf-8")
        assert "PCHK-DEL:abc123" in written

    async def test_delete_chunk_exception(self):
        a = _make_adapter()

        async def fake_connect():
            pass
        a._connect = fake_connect
        a._writer = MagicMock()
        a._writer.write = MagicMock(side_effect=Exception("broken pipe"))

        ref = StoredChunkRef(adapter_name="irc", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        writer = _mock_writer()
        a._writer = writer

        line1 = b":bot!u@h PRIVMSG #chunks :PCHK-ENT:id1:data1\r\n"
        line2 = b":bot!u@h PRIVMSG #chunks :PCHK-ENT:id2:data2\r\n"

        reader = AsyncMock()
        reader.readline = AsyncMock(
            side_effect=[line1, line2, asyncio.TimeoutError()],
        )
        a._reader = reader

        async def fake_connect():
            pass
        a._connect = fake_connect

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert refs[0].location == "id1"
        assert refs[1].location == "id2"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        writer = _mock_writer()
        a._writer = writer
        a._reader = AsyncMock()
        a._reader.readline = AsyncMock(side_effect=asyncio.TimeoutError())

        async def fake_connect():
            pass
        a._connect = fake_connect

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        reader = _mock_reader([":server PONG :picard-health"])
        writer = _mock_writer()
        a._writer = writer
        a._reader = reader

        async def fake_connect():
            pass
        a._connect = fake_connect

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()

        async def fail_connect():
            raise ConnectionRefusedError("refused")
        a._connect = fail_connect

        assert await a.health_check() is False

    async def test_ping_pong_handling(self):
        a = _make_adapter()
        reader = _mock_reader([
            "PING :server123",
            ":server 001 picard-bot :Welcome",
            ":picard-bot!u@h JOIN #chunks",
        ])
        writer = _mock_writer()

        with patch("asyncio.open_connection", return_value=(reader, writer)):
            a._reader = None
            a._writer = None
            await a._connect()

        calls = [c[0][0].decode("utf-8") for c in writer.write.call_args_list]
        pong_calls = [c for c in calls if c.startswith("PONG")]
        assert any("server123" in c for c in pong_calls)

    async def test_send_raw(self):
        a = _make_adapter()
        writer = _mock_writer()
        a._writer = writer
        a._send_raw("PRIVMSG #test :hello")
        writer.write.assert_called_once_with(b"PRIVMSG #test :hello\r\n")

    async def test_connect_with_password(self):
        a = _make_adapter(password="secret")
        reader = _mock_reader([
            ":server 001 picard-bot :Welcome",
            ":picard-bot!u@h JOIN #chunks",
        ])
        writer = _mock_writer()

        with patch("asyncio.open_connection", return_value=(reader, writer)):
            await a._connect()

        calls = [c[0][0].decode("utf-8") for c in writer.write.call_args_list]
        assert calls[0] == "PASS secret\r\n"
        assert calls[1] == "NICK picard-bot\r\n"
