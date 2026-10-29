"""NATS JetStream adapter -- stores chunks in a NATS JetStream stream."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "nats-jetstream"
_STREAM = "PICARD"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class NatsJetstreamAdapter(Adapter):

    def __init__(self, *, servers: str = "nats://127.0.0.1:4222") -> None:
        self._servers = servers
        self._nc = None
        self._js = None
        self._reg: dict[str, str] = {}

    async def _get_js(self):
        if self._nc is None:
            import nats
            self._nc = await nats.connect(servers=self._servers)
            self._js = self._nc.jetstream()
            try:
                await self._js.find_stream(_STREAM)
            except Exception:
                await self._js.add_stream(
                    name=_STREAM,
                    subjects=["picard.>"],
                    max_age=30 * 24 * 3600,
                )
        return self._js

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=1_048_576,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10000,
            tags={"requires": "nats-py", "substrate": "nats-jetstream-stream"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        subject = f"picard.{manifest_id}.{token}"
        self._reg[token] = encoded_chunk
        js = await self._get_js()
        ack = await js.publish(subject, encoded_chunk.encode("utf-8"))
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "subject": subject,
                                 "seq": ack.seq,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        js = await self._get_js()
        msg = await js.get_msg(_STREAM, loc["seq"])
        chunk = msg.data.decode("utf-8")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        seq = loc.get("seq")
        if seq is not None:
            try:
                js = await self._get_js()
                await js.delete_msg(_STREAM, seq)
            except Exception:
                pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "subject": f"picard.{manifest_id}.{token}",
                                         "seq": None,
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            await self._get_js()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None
