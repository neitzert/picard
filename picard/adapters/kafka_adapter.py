"""Kafka adapter -- stores chunks as messages in a Kafka topic.

Each chunk becomes a Kafka message with the chunk key as the message
key and the encoded data as the value.  The location records the
topic, partition, and offset for precise retrieval.

Requires: pip install picard[kafka]  (aiokafka)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class KafkaAdapter(Adapter):
    """Scatter chunks into Kafka topic partitions."""

    def __init__(
        self,
        bootstrap_servers: str | list[str] = "localhost:9092",
        *,
        topic: str = "picard-chunks",
        client_id: str = "picard",
        max_chunk_bytes: int = 1_048_576,
    ) -> None:
        if isinstance(bootstrap_servers, str):
            bootstrap_servers = [bootstrap_servers]
        self._servers = bootstrap_servers
        self._topic = topic
        self._client_id = client_id
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="kafka",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=10000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from aiokafka import AIOKafkaProducer

        key = self._chunk_key(encoded_chunk)

        producer = AIOKafkaProducer(
            bootstrap_servers=self._servers,
            client_id=self._client_id,
        )
        await producer.start()
        try:
            result = await producer.send_and_wait(
                self._topic,
                value=encoded_chunk.encode("utf-8"),
                key=key.encode("utf-8"),
            )
            location = f"kafka://{self._topic}/{result.partition}/{result.offset}"
        finally:
            await producer.stop()

        return StoredChunkRef(adapter_name="kafka", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        from aiokafka import AIOKafkaConsumer, TopicPartition

        parts = ref.location.removeprefix("kafka://").split("/")
        topic, partition, offset = parts[0], int(parts[1]), int(parts[2])

        tp = TopicPartition(topic, partition)
        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._servers,
            client_id=self._client_id,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            consumer.assign([tp])
            await consumer.seek(tp, offset)
            msg = await consumer.getone(tp)
            return msg.value.decode("utf-8")
        finally:
            await consumer.stop()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        # Kafka doesn't support per-message deletion; return False.
        # Chunks are removed only when topic retention expires.
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from aiokafka import AIOKafkaConsumer, TopicPartition

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._servers,
            client_id=self._client_id,
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            partitions = consumer.partitions_for_topic(self._topic) or set()
            tps = [TopicPartition(self._topic, p) for p in partitions]
            consumer.assign(tps)
            await consumer.seek_to_beginning(*tps)

            refs: list[StoredChunkRef] = []
            end_offsets = await consumer.end_offsets(tps)

            for tp in tps:
                end = end_offsets[tp]
                while consumer.position(tp) < end:
                    msg = await consumer.getone(tp)
                    key_str = msg.key.decode("utf-8") if msg.key else ""
                    if key_str.startswith(f"{manifest_id}/"):
                        loc = f"kafka://{self._topic}/{msg.partition}/{msg.offset}"
                        refs.append(StoredChunkRef(adapter_name="kafka", location=loc))
            return refs
        finally:
            await consumer.stop()

    async def health_check(self) -> bool:
        from aiokafka import AIOKafkaProducer

        try:
            producer = AIOKafkaProducer(
                bootstrap_servers=self._servers,
                client_id=self._client_id,
            )
            await producer.start()
            await producer.stop()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        from aiokafka import AIOKafkaConsumer, TopicPartition

        try:
            consumer = AIOKafkaConsumer(
                bootstrap_servers=self._servers,
                client_id=self._client_id,
                enable_auto_commit=False,
            )
            await consumer.start()
            try:
                partitions = consumer.partitions_for_topic(self._topic) or set()
                tps = [TopicPartition(self._topic, p) for p in partitions]
                if not tps:
                    return {
                        "used_bytes": None,
                        "free_bytes": None,
                        "total_bytes": None,
                        "chunk_count": 0,
                    }
                begin = await consumer.beginning_offsets(tps)
                end = await consumer.end_offsets(tps)
                total_msgs = sum(end[tp] - begin[tp] for tp in tps)
                return {
                    "used_bytes": None,
                    "free_bytes": None,
                    "total_bytes": None,
                    "chunk_count": total_msgs,
                }
            finally:
                await consumer.stop()
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
