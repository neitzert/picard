"""Tests for the AMQP adapter — mocked, no broker needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.amqp import AmqpAdapter
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


def _make_adapter(**kwargs) -> AmqpAdapter:
    defaults = dict(url="amqp://guest:guest@localhost/")
    defaults.update(kwargs)
    return AmqpAdapter(**defaults)


def _wired_adapter() -> tuple[AmqpAdapter, AsyncMock, AsyncMock, AsyncMock]:
    a = _make_adapter()
    channel = AsyncMock()
    exchange = AsyncMock()
    connection = AsyncMock()
    a._connection = connection
    a._channel = channel
    a._exchange = exchange
    return a, channel, exchange, connection


@pytest.mark.asyncio
class TestAmqpAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "amqp"
        assert p.max_chunk_bytes == 200_000
        assert p.persistent is True
        assert p.ordered is True
        assert p.rate_limit_per_min == 1000

    async def test_queue_name(self):
        a = _make_adapter()
        assert a._queue_name("abc123") == "picard.abc123"

    async def test_write_chunk(self):
        a, channel, exchange, _ = _wired_adapter()
        queue = AsyncMock()
        channel.declare_queue.return_value = queue
        a._make_message = AsyncMock(return_value=MagicMock())

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "amqp"
        assert len(ref.location) == 16
        channel.declare_queue.assert_awaited_once()
        queue.bind.assert_awaited_once()
        exchange.publish.assert_awaited_once()

    async def test_read_chunk(self):
        a, channel, _, _ = _wired_adapter()
        enc = _sample_encoded()

        msg = AsyncMock()
        msg.body = enc.encode("utf-8")
        msg.nack = AsyncMock()

        queue = AsyncMock()
        queue.get.return_value = msg
        channel.declare_queue.return_value = queue

        ref = StoredChunkRef(adapter_name="amqp", location="abc123")
        result = await a.read_chunk(ref)

        assert result == enc
        msg.nack.assert_awaited_once_with(requeue=True)

    async def test_read_chunk_empty_queue(self):
        a, channel, _, _ = _wired_adapter()

        queue = AsyncMock()
        queue.get.return_value = None
        channel.declare_queue.return_value = queue

        ref = StoredChunkRef(adapter_name="amqp", location="abc123")
        with pytest.raises(ValueError, match="no message"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a, channel, _, _ = _wired_adapter()

        queue = AsyncMock()
        channel.declare_queue.return_value = queue

        ref = StoredChunkRef(adapter_name="amqp", location="abc123")
        assert await a.delete_chunk(ref) is True
        queue.delete.assert_awaited_once()

    async def test_delete_chunk_not_found(self):
        a, channel, _, _ = _wired_adapter()
        channel.declare_queue.side_effect = Exception("queue not found")

        ref = StoredChunkRef(adapter_name="amqp", location="abc123")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks_returns_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a, _, _, connection = _wired_adapter()
        assert await a.health_check() is True

    async def test_health_check_no_connection(self):
        a = _make_adapter()
        with patch("picard.adapters.amqp.AmqpAdapter._ensure_connection", new_callable=AsyncMock) as mock_conn:
            mock_conn.side_effect = Exception("refused")
            assert await a.health_check() is False

    async def test_custom_exchange_name(self):
        a = _make_adapter(exchange_name="my-exchange")
        assert a._exchange_name == "my-exchange"

    async def test_custom_max_bytes(self):
        a = _make_adapter(max_chunk_bytes=500_000)
        assert a.profile().max_chunk_bytes == 500_000

    async def test_write_generates_unique_ids(self):
        a, channel, exchange, _ = _wired_adapter()
        queue = AsyncMock()
        channel.declare_queue.return_value = queue
        a._make_message = AsyncMock(return_value=MagicMock())

        enc = _sample_encoded()
        ref1 = await a.write_chunk(enc)
        ref2 = await a.write_chunk(enc)

        assert ref1.location != ref2.location
