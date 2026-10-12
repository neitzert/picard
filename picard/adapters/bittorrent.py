"""BitTorrent DHT adapter — stores chunks as BEP 44 immutable items.

Each chunk is bencoded and stored in the DHT keyed by the SHA-1 hash
of the bencoded value.  A cooperating DHT node network caches items;
they expire without periodic re-announcement.

Requires: no extra dependencies (stdlib asyncio + hashlib)
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from typing import Sequence
from uuid import uuid4

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_DEFAULT_BOOTSTRAP = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
]


def _bencode(obj) -> bytes:
    if isinstance(obj, int):
        return f"i{obj}e".encode()
    if isinstance(obj, bytes):
        return f"{len(obj)}:".encode() + obj
    if isinstance(obj, str):
        encoded = obj.encode()
        return f"{len(encoded)}:".encode() + encoded
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(i) for i in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0] if isinstance(kv[0], bytes) else kv[0].encode())
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(obj)}")


def _bdecode(data: bytes):
    value, _ = _bdecode_inner(data, 0)
    return value


def _bdecode_inner(data: bytes, idx: int):
    ch = data[idx:idx + 1]
    if ch == b"i":
        end = data.index(b"e", idx + 1)
        return int(data[idx + 1:end]), end + 1
    if ch == b"l":
        idx += 1
        items = []
        while data[idx:idx + 1] != b"e":
            val, idx = _bdecode_inner(data, idx)
            items.append(val)
        return items, idx + 1
    if ch == b"d":
        idx += 1
        result = {}
        while data[idx:idx + 1] != b"e":
            key, idx = _bdecode_inner(data, idx)
            val, idx = _bdecode_inner(data, idx)
            result[key] = val
        return result, idx + 1
    if ch and ch.isdigit() or ch == b"0":
        colon = data.index(b":", idx)
        length = int(data[idx:colon])
        start = colon + 1
        return data[start:start + length], start + length
    raise ValueError(f"unexpected byte at {idx}: {ch!r}")


class BitTorrentAdapter(Adapter):
    """Scatter chunks into the BitTorrent DHT via BEP 44."""

    def __init__(
        self,
        *,
        bootstrap_nodes: list[tuple[str, int]] | None = None,
        port: int = 6881,
        max_chunk_bytes: int = 900,
    ) -> None:
        self._bootstrap = bootstrap_nodes or list(_DEFAULT_BOOTSTRAP)
        self._port = port
        self._max = max_chunk_bytes
        self._node_id = uuid4().bytes[:20]
        self._transport: object | None = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bittorrent-dht",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
            ttl_seconds=7200,
        )

    async def _send_recv(self, data: bytes, addr: tuple[str, int]) -> bytes:
        loop = asyncio.get_running_loop()
        sock = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            remote_addr=addr,
        )
        transport, _ = sock
        try:
            fut: asyncio.Future[bytes] = loop.create_future()

            class _Proto(asyncio.DatagramProtocol):
                def datagram_received(self, d, a):
                    if not fut.done():
                        fut.set_result(d)

            transport.close()
            transport, _ = await loop.create_datagram_endpoint(
                _Proto, remote_addr=addr,
            )
            transport.sendto(data)
            return await asyncio.wait_for(fut, timeout=5.0)
        finally:
            transport.close()

    async def _put(self, value: bytes) -> bytes:
        target = hashlib.sha1(value).digest()
        txn = uuid4().bytes[:2]
        msg = _bencode({
            b"t": txn,
            b"y": b"q",
            b"q": b"put",
            b"a": {
                b"id": self._node_id,
                b"v": value,
            },
        })
        for node in self._bootstrap:
            try:
                await self._send_recv(msg, node)
                break
            except Exception:
                continue
        return target

    async def _get(self, target: bytes) -> bytes:
        txn = uuid4().bytes[:2]
        msg = _bencode({
            b"t": txn,
            b"y": b"q",
            b"q": b"get",
            b"a": {
                b"id": self._node_id,
                b"target": target,
            },
        })
        for node in self._bootstrap:
            try:
                resp_data = await self._send_recv(msg, node)
                resp = _bdecode(resp_data)
                return resp[b"r"][b"v"]
            except Exception:
                continue
        raise ConnectionError("no DHT node returned the value")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        value = _bencode(encoded_chunk.encode())
        target = await self._put(value)
        return StoredChunkRef(
            adapter_name="bittorrent-dht",
            location=target.hex(),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        target = bytes.fromhex(ref.location)
        raw = await self._get(target)
        if isinstance(raw, bytes):
            return raw.decode()
        return str(raw)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        txn = uuid4().bytes[:2]
        msg = _bencode({
            b"t": txn,
            b"y": b"q",
            b"q": b"ping",
            b"a": {b"id": self._node_id},
        })
        for node in self._bootstrap:
            try:
                await self._send_recv(msg, node)
                return True
            except Exception:
                continue
        return False
