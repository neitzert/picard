"""STOMP adapter -- stores chunks as messages on STOMP queues.

Works with any STOMP-compatible broker: ActiveMQ, RabbitMQ (STOMP
plugin), Apollo, etc.  Chunks are sent with a picard-key header
for retrieval, and the adapter maintains a local receive cache
for read-back.

Requires: pip install picard-store[stomp]  (stomp.py)
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class StompAdapter(Adapter):
    """Scatter chunks onto STOMP queues/topics."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 61613,
        *,
        username: str = "guest",
        password: str = "guest",
        destination: str = "/queue/picard",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._destination = destination
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="stomp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=1000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    def _connect(self):
        import stomp

        conn = stomp.Connection([(self._host, self._port)])
        conn.connect(self._username, self._password, wait=True)
        return conn

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)

        def _send():
            conn = self._connect()
            try:
                conn.send(
                    destination=self._destination,
                    body=encoded_chunk,
                    headers={
                        "picard-key": key,
                        "persistent": "true",
                        "content-type": "text/plain",
                    },
                )
            finally:
                conn.disconnect()

        await asyncio.to_thread(_send)

        with self._lock:
            self._cache[key] = encoded_chunk

        location = f"stomp://{self._host}/{self._destination}/{key}"
        return StoredChunkRef(adapter_name="stomp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        parts = ref.location.removeprefix(f"stomp://{self._host}/")
        dest_and_key = parts.rsplit("/", 3)
        key = "/".join(dest_and_key[-3:]) if len(dest_and_key) > 3 else dest_and_key[-1]

        with self._lock:
            if key in self._cache:
                return self._cache[key]

        received: dict[str, str] = {}
        event = threading.Event()

        def _subscribe():
            import stomp

            class Listener(stomp.ConnectionListener):
                def on_message(self, frame):
                    msg_key = frame.headers.get("picard-key", "")
                    received[msg_key] = frame.body
                    if msg_key == key:
                        event.set()

            conn = self._connect()
            conn.set_listener("picard", Listener())
            conn.subscribe(
                destination=self._destination,
                id="picard-read",
                ack="auto",
                headers={"selector": f"picard-key='{key}'"},
            )
            event.wait(timeout=10)
            conn.disconnect()

        await asyncio.to_thread(_subscribe)

        with self._lock:
            self._cache.update(received)

        if key in received:
            return received[key]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.removeprefix(f"stomp://{self._host}/")
        dest_and_key = parts.rsplit("/", 3)
        key = "/".join(dest_and_key[-3:]) if len(dest_and_key) > 3 else dest_and_key[-1]

        with self._lock:
            existed = key in self._cache
            self._cache.pop(key, None)
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}/"
        with self._lock:
            return [
                StoredChunkRef(
                    adapter_name="stomp",
                    location=f"stomp://{self._host}/{self._destination}/{k}",
                )
                for k in self._cache
                if k.startswith(prefix)
            ]

    async def health_check(self) -> bool:
        try:
            conn = await asyncio.to_thread(self._connect)
            conn.disconnect()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        with self._lock:
            used = sum(len(v.encode()) for v in self._cache.values())
            count = len(self._cache)
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": count,
        }
