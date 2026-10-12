"""MQTT adapter -- stores chunks as retained messages on an MQTT broker.

Each chunk becomes a retained message on topic:
    picard/{manifest_id}/{block_index}/{chunk_index}

Requires: pip install picard[mqtt]
"""

from __future__ import annotations

import asyncio
import threading
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MqttAdapter(Adapter):
    """Scatter chunks into retained MQTT messages."""

    def __init__(
        self,
        broker_host: str,
        *,
        broker_port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._username = username
        self._password = password
        self._max = max_chunk_bytes
        self._client: object | None = None
        self._lock = threading.Lock()

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self._username:
            client.username_pw_set(self._username, self._password)
        client.connect(self._broker_host, self._broker_port)
        client.loop_start()
        self._client = client
        return client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="mqtt",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    @staticmethod
    def _topic(manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"picard/{manifest_id}/{block_index}/{chunk_index}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk = Chunk.decode(encoded_chunk)
        topic = self._topic(chunk.manifest_id, chunk.block_index, chunk.chunk_index)

        def _write():
            client = self._ensure_client()
            info = client.publish(topic, encoded_chunk.encode("utf-8"), retain=True, qos=1)
            info.wait_for_publish(timeout=10)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="mqtt", location=topic)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        def _read():
            client = self._ensure_client()
            result: dict[str, str] = {}
            event = threading.Event()

            def on_msg(_client, _userdata, msg):
                result["payload"] = msg.payload.decode("utf-8")
                event.set()

            with self._lock:
                client.message_callback_add(ref.location, on_msg)
                client.subscribe(ref.location, qos=1)

            if not event.wait(timeout=10):
                with self._lock:
                    client.unsubscribe(ref.location)
                    client.message_callback_remove(ref.location)
                raise TimeoutError(f"no retained message on {ref.location}")

            with self._lock:
                client.unsubscribe(ref.location)
                client.message_callback_remove(ref.location)

            return result["payload"]

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        def _delete():
            client = self._ensure_client()
            info = client.publish(ref.location, b"", retain=True, qos=1)
            info.wait_for_publish(timeout=10)

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """Subscribe to the wildcard topic and collect retained messages."""

        def _list():
            client = self._ensure_client()
            refs: list[StoredChunkRef] = []
            last_msg = threading.Event()
            wildcard = f"picard/{manifest_id}/#"

            def on_msg(_client, _userdata, msg):
                if msg.payload:
                    refs.append(
                        StoredChunkRef(adapter_name="mqtt", location=msg.topic)
                    )
                last_msg.set()

            with self._lock:
                client.message_callback_add(wildcard, on_msg)
                client.subscribe(wildcard, qos=1)

            last_msg.wait(timeout=3)

            with self._lock:
                client.unsubscribe(wildcard)
                client.message_callback_remove(wildcard)

            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        try:
            self._ensure_client()
            return True
        except Exception:
            return False
