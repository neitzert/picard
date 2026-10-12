"""AMQP 0-9-1 adapter — stores chunks as persistent messages in per-chunk queues.

Each chunk gets its own durable queue named ``picard.{msg_id}``.  A single
direct exchange fans writes to the correct queue.  Reads use ``basic_get``
with requeue so the message stays available for future reads.

Requires: pip install picard-store[amqp]   (aio-pika)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class AmqpAdapter(Adapter):
    """Scatter chunks into AMQP queues via aio-pika."""

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        *,
        exchange_name: str = "picard",
        max_chunk_bytes: int = 200_000,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._max = max_chunk_bytes
        self._connection: object | None = None
        self._channel: object | None = None
        self._exchange: object | None = None

    async def _ensure_connection(self):
        if self._connection is not None:
            return
        import aio_pika

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )

    def _queue_name(self, msg_id: str) -> str:
        return f"picard.{msg_id}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="amqp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=1000,
        )

    async def _make_message(self, body: bytes, msg_id: str):
        import aio_pika

        return aio_pika.Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=msg_id,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        await self._ensure_connection()

        msg_id = uuid.uuid4().hex[:16]
        queue_name = self._queue_name(msg_id)

        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._exchange, routing_key=msg_id)

        message = await self._make_message(
            encoded_chunk.encode("utf-8"), msg_id,
        )
        await self._exchange.publish(message, routing_key=msg_id)

        return StoredChunkRef(adapter_name="amqp", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        await self._ensure_connection()

        queue_name = self._queue_name(ref.location)
        queue = await self._channel.declare_queue(queue_name, durable=True)

        msg = await queue.get(fail=False)
        if msg is None:
            raise ValueError(f"no message in queue {queue_name}")

        body = msg.body.decode("utf-8")
        await msg.nack(requeue=True)
        return body

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        await self._ensure_connection()

        queue_name = self._queue_name(ref.location)
        try:
            queue = await self._channel.declare_queue(
                queue_name, durable=True, passive=True,
            )
            await queue.delete()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            await self._ensure_connection()
            return self._connection is not None
        except Exception:
            return False
