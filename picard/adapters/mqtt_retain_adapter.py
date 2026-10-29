"""MQTT retained message adapter -- stores chunks as MQTT retained messages.

Each write_chunk publishes the encoded chunk to topic picard/{token} with the
retain flag set. The broker holds the last retained message per topic, making
it retrievable by subscribing to that topic. Persistent across reconnects.

Requires: pip install aiomqtt
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "mqtt-retain"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class MqttRetainAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str = "",
        password: str = "",
        prefix: str = "picard",
        tls: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username or None
        self._password = password or None
        self._prefix = prefix
        self._tls = tls
        self._reg: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=262144,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "aiomqtt", "substrate": "mqtt-retained-message"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import aiomqtt

        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        topic = f"{self._prefix}/{token}"

        kwargs: dict = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password
        if self._tls:
            kwargs["tls_context"] = True

        async with aiomqtt.Client(self._host, port=self._port, **kwargs) as client:
            await client.publish(topic, payload=encoded_chunk.encode(), retain=True, qos=1)

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "topic": topic,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import aiomqtt

        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        topic = loc["topic"]
        kwargs: dict = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password

        chunk = ""
        async with aiomqtt.Client(self._host, port=self._port, **kwargs) as client:
            await client.subscribe(topic)
            try:
                async with asyncio.timeout(5.0):
                    async for msg in client.messages:
                        chunk = msg.payload.decode()
                        break
            except TimeoutError:
                pass

        if not chunk:
            raise KeyError(f"mqtt-retain chunk not found: {token}")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import aiomqtt

        loc = json.loads(ref.location)
        token = loc["token"]
        topic = loc.get("topic", f"{self._prefix}/{token}")
        self._reg.pop(token, None)

        kwargs: dict = {}
        if self._username:
            kwargs["username"] = self._username
        if self._password:
            kwargs["password"] = self._password

        # Publish empty retained message to clear it
        async with aiomqtt.Client(self._host, port=self._port, **kwargs) as client:
            await client.publish(topic, payload=b"", retain=True, qos=1)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "topic": f"{self._prefix}/{token}",
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            import aiomqtt
            kwargs: dict = {}
            if self._username:
                kwargs["username"] = self._username
            if self._password:
                kwargs["password"] = self._password
            async with asyncio.timeout(3.0):
                async with aiomqtt.Client(self._host, port=self._port, **kwargs):
                    return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
