"""Tests for the Kafka adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.kafka_adapter import KafkaAdapter


@pytest.fixture
def adapter():
    return KafkaAdapter(bootstrap_servers="localhost:9092", topic="test-topic")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "kafka"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 10000


@pytest.mark.asyncio
class TestWriteChunk:
    async def test_write_returns_ref(self, adapter):
        mock_producer = AsyncMock()
        result = SimpleNamespace(partition=0, offset=42)
        mock_producer.send_and_wait = AsyncMock(return_value=result)
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()

        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaProducer = MagicMock(return_value=mock_producer)
        sys.modules["aiokafka"] = mock_mod
        try:
            ref = await adapter.write_chunk("PCHK:1:test123:0:0:1:dGVzdA==")
            assert ref.adapter_name == "kafka"
            assert "kafka://test-topic/0/42" == ref.location
        finally:
            del sys.modules["aiokafka"]

    async def test_write_opaque_data(self, adapter):
        mock_producer = AsyncMock()
        result = SimpleNamespace(partition=1, offset=10)
        mock_producer.send_and_wait = AsyncMock(return_value=result)
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()

        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaProducer = MagicMock(return_value=mock_producer)
        sys.modules["aiokafka"] = mock_mod
        try:
            ref = await adapter.write_chunk("deadbeef0102030405")
            assert ref.adapter_name == "kafka"
            assert ref.location == "kafka://test-topic/1/10"
        finally:
            del sys.modules["aiokafka"]


@pytest.mark.asyncio
class TestReadChunk:
    async def test_read_by_location(self, adapter):
        mock_consumer = AsyncMock()
        msg = SimpleNamespace(value=b"PCHK:1:test123:0:0:1:dGVzdA==")
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.assign = MagicMock()
        mock_consumer.seek = AsyncMock()
        mock_consumer.getone = AsyncMock(return_value=msg)

        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaConsumer = MagicMock(return_value=mock_consumer)
        mock_mod.TopicPartition = lambda t, p: SimpleNamespace(topic=t, partition=p)
        sys.modules["aiokafka"] = mock_mod
        try:
            from picard.adapters.base import StoredChunkRef
            ref = StoredChunkRef(adapter_name="kafka", location="kafka://test-topic/0/42")
            result = await adapter.read_chunk(ref)
            assert result == "PCHK:1:test123:0:0:1:dGVzdA=="
        finally:
            del sys.modules["aiokafka"]


@pytest.mark.asyncio
class TestDeleteChunk:
    async def test_delete_returns_false(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="kafka", location="kafka://test-topic/0/42")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
        mock_producer = AsyncMock()
        mock_producer.start = AsyncMock()
        mock_producer.stop = AsyncMock()

        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaProducer = MagicMock(return_value=mock_producer)
        sys.modules["aiokafka"] = mock_mod
        try:
            assert await adapter.health_check() is True
        finally:
            del sys.modules["aiokafka"]

    async def test_unhealthy(self, adapter):
        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaProducer = MagicMock(side_effect=Exception("refused"))
        sys.modules["aiokafka"] = mock_mod
        try:
            assert await adapter.health_check() is False
        finally:
            del sys.modules["aiokafka"]


@pytest.mark.asyncio
class TestCapacity:
    async def test_returns_chunk_count(self, adapter):
        mock_consumer = AsyncMock()
        mock_consumer.start = AsyncMock()
        mock_consumer.stop = AsyncMock()
        mock_consumer.partitions_for_topic = MagicMock(return_value={0})
        mock_consumer.assign = MagicMock()

        tp_key = "tp:test-topic:0"
        mock_consumer.beginning_offsets = AsyncMock(return_value={tp_key: 0})
        mock_consumer.end_offsets = AsyncMock(return_value={tp_key: 100})

        import sys
        mock_mod = MagicMock()
        mock_mod.AIOKafkaConsumer = MagicMock(return_value=mock_consumer)
        mock_mod.TopicPartition = lambda t, p: tp_key
        sys.modules["aiokafka"] = mock_mod
        try:
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 100
        finally:
            del sys.modules["aiokafka"]
