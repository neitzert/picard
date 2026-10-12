"""ZeroMQ adapter -- stores chunks via ZeroMQ REQ/REP to a backing store.

Chunks are sent as two-frame messages [key, data] over a REQ socket
to a server that stores and retrieves them.  The server must implement
the following protocol:

    PUT <key> <data>  -> OK
    GET <key>         -> <data>
    DEL <key>         -> OK | NOTFOUND
    LIST <prefix>     -> <key1>\n<key2>\n...

Requires: pip install picard[zeromq]  (pyzmq)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ZeromqAdapter(Adapter):
    """Scatter chunks via ZeroMQ REQ/REP messages."""

    def __init__(
        self,
        endpoint: str = "tcp://localhost:5555",
        *,
        max_chunk_bytes: int = 1_048_576,
        timeout_ms: int = 5000,
    ) -> None:
        self._endpoint = endpoint
        self._max = max_chunk_bytes
        self._timeout = timeout_ms

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="zeromq",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=10000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import zmq
        import zmq.asyncio

        key = self._chunk_key(encoded_chunk)

        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self._timeout)
        sock.setsockopt(zmq.SNDTIMEO, self._timeout)
        sock.connect(self._endpoint)
        try:
            await sock.send_multipart([
                b"PUT",
                key.encode("utf-8"),
                encoded_chunk.encode("utf-8"),
            ])
            reply = await sock.recv()
            if reply != b"OK":
                raise RuntimeError(f"ZMQ PUT failed: {reply!r}")
        finally:
            sock.close()

        location = f"zmq://{self._endpoint}/{key}"
        return StoredChunkRef(adapter_name="zeromq", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import zmq
        import zmq.asyncio

        prefix = f"zmq://{self._endpoint}/"
        key = ref.location.removeprefix(prefix)

        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self._timeout)
        sock.setsockopt(zmq.SNDTIMEO, self._timeout)
        sock.connect(self._endpoint)
        try:
            await sock.send_multipart([
                b"GET",
                key.encode("utf-8"),
            ])
            reply = await sock.recv()
            return reply.decode("utf-8")
        finally:
            sock.close()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import zmq
        import zmq.asyncio

        prefix = f"zmq://{self._endpoint}/"
        key = ref.location.removeprefix(prefix)

        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self._timeout)
        sock.setsockopt(zmq.SNDTIMEO, self._timeout)
        sock.connect(self._endpoint)
        try:
            await sock.send_multipart([
                b"DEL",
                key.encode("utf-8"),
            ])
            reply = await sock.recv()
            return reply == b"OK"
        finally:
            sock.close()

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import zmq
        import zmq.asyncio

        ctx = zmq.asyncio.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, self._timeout)
        sock.setsockopt(zmq.SNDTIMEO, self._timeout)
        sock.connect(self._endpoint)
        try:
            await sock.send_multipart([
                b"LIST",
                f"{manifest_id}/".encode("utf-8"),
            ])
            reply = await sock.recv()
            keys = reply.decode("utf-8").strip().split("\n")
            return [
                StoredChunkRef(
                    adapter_name="zeromq",
                    location=f"zmq://{self._endpoint}/{k}",
                )
                for k in keys
                if k
            ]
        finally:
            sock.close()

    async def health_check(self) -> bool:
        import zmq
        import zmq.asyncio

        try:
            ctx = zmq.asyncio.Context.instance()
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, self._timeout)
            sock.setsockopt(zmq.SNDTIMEO, self._timeout)
            sock.connect(self._endpoint)
            try:
                await sock.send_multipart([b"PING"])
                await sock.recv()
                return True
            finally:
                sock.close()
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
