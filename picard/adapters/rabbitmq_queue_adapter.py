"""RabbitMQ queue adapter -- stores chunks as persistent messages in dedicated queues.

Each write_chunk declares a durable queue named picard.{token} and publishes
the encoded chunk as a persistent message. Cold reads consume and re-publish.

Requires: pip install aio-pika
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "rabbitmq-queue"


def _manifest_id(chunk: str) -> str:
    try:
        parts = chunk.split(":")
        return parts[2] if parts[0] == "PCHK" and len(parts) >= 3 else ""
    except Exception:
        return ""


class RabbitmqQueueAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 5672,
                 user: str = "guest", password: str = "guest",
                 vhost: str = "/") -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._vhost = vhost
        self._conn = None
        self._reg: dict[str, str] = {}

    async def _get_conn(self):
        if self._conn is None or self._conn.is_closed:
            import aio_pika
            self._conn = await aio_pika.connect_robust(
                host=self._host, port=self._port,
                login=self._user, password=self._password,
                virtualhost=self._vhost,
            )
        return self._conn

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            tags={"requires": "aio-pika", "substrate": "rabbitmq-queue-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aio_pika
        token = uuid.uuid4().hex[:16]
        queue_name = f"picard.{token}"
        conn = await self._get_conn()
        async with conn.channel() as ch:
            await ch.declare_queue(queue_name, durable=True)
            await ch.default_exchange.publish(
                aio_pika.Message(
                    body=encoded_chunk.encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=queue_name,
            )
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                   "queue": queue_name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        import aio_pika
        conn = await self._get_conn()
        async with conn.channel() as ch:
            queue = await ch.declare_queue(loc["queue"], durable=True, passive=True)
            msg = await queue.get(fail=False)
            if msg is None:
                raise KeyError(f"rabbitmq-queue chunk not found: {token}")
            chunk = msg.body.decode()
            await msg.ack()
            # Re-publish so the message remains available
            await ch.default_exchange.publish(
                aio_pika.Message(
                    body=msg.body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=loc["queue"],
            )
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        conn = await self._get_conn()
        async with conn.channel() as ch:
            q = await ch.get_queue(loc["queue"])
            await q.purge()
            await q.delete()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "queue": f"picard.{token}"}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            conn = await self._get_conn()
            return not conn.is_closed
        except Exception:
            return False

    async def close(self) -> None:
        if self._conn is not None and not self._conn.is_closed:
            await self._conn.close()
        self._conn = None
