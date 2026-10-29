"""Kafka consumer-record adapter -- stores chunks as Kafka topic messages.

Each write_chunk produces a message to a dedicated Kafka topic. The offset
is stored in the location ref. Cold reads seek the consumer to the stored
offset and fetch that specific record.

Requires: pip install aiokafka
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "kafka-consumer-record"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class KafkaConsumerRecordAdapter(Adapter):

    def __init__(
        self,
        *,
        bootstrap_servers: str = "127.0.0.1:9092",
        topic: str = "picard-chunks",
        group_id: str = "picard-reader",
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._producer = None
        self._reg: dict[str, str] = {}

    async def _get_producer(self):
        if self._producer is None:
            from aiokafka import AIOKafkaProducer
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers
            )
            await self._producer.start()
        return self._producer

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=True,
            rate_limit_per_min=10000,
            tags={"requires": "aiokafka", "substrate": "kafka-topic-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        producer = await self._get_producer()

        meta = await producer.send_and_wait(
            self._topic,
            value=encoded_chunk.encode(),
            key=token.encode(),
        )

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({
                "token": token,
                "topic": self._topic,
                "partition": meta.partition,
                "offset": meta.offset,
                "manifest": manifest_id,
            }),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        from aiokafka import AIOKafkaConsumer, TopicPartition

        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        tp = TopicPartition(loc["topic"], loc["partition"])
        offset = loc["offset"]

        consumer = AIOKafkaConsumer(
            bootstrap_servers=self._bootstrap_servers,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            consumer.assign([tp])
            await consumer.seek(tp, offset)
            record = await consumer.getone(tp)
            chunk = record.value.decode()
        finally:
            await consumer.stop()

        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        # Kafka records are immutable; TTL or log compaction handles cleanup
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "topic": self._topic,
                                         "partition": 0,
                                         "offset": -1,
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            from aiokafka.admin import AIOKafkaAdminClient
            admin = AIOKafkaAdminClient(
                bootstrap_servers=self._bootstrap_servers
            )
            await admin.start()
            await admin.close()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
