"""NetFlow adapter — chunks encoded in NetFlow v9 template data records.

NetFlow traffic is ubiquitous in enterprise networks. This adapter
encodes chunk data into vendor-specific fields in NetFlow v9 data
records and sends them to a collector. Chunk data blends in
with legitimate network telemetry.

Requires: UDP socket access.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import time
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class NetflowAdapter(Adapter):
    """Store chunks as NetFlow v9 data records with vendor-specific fields."""

    NETFLOW_V9_VERSION = 9
    TEMPLATE_ID = 256
    VENDOR_FIELD_TYPE = 32768  # private enterprise field

    def __init__(
        self,
        collector_host: str = "127.0.0.1",
        collector_port: int = 2055,
        source_id: int = 1,
        *,
        max_chunk_bytes: int = 1400,
    ) -> None:
        self._host = collector_host
        self._port = collector_port
        self._source_id = source_id
        self._max = max_chunk_bytes
        self._seq = 0
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="netflow",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=600,
            ttl_seconds=300,
        )

    def _build_header(self, count: int) -> bytes:
        self._seq += 1
        return struct.pack(
            "!HHIII",
            self.NETFLOW_V9_VERSION,
            count,
            int(time.time() * 1000) & 0xFFFFFFFF,
            int(time.time()),
            self._source_id,
        )

    def _build_template_flowset(self) -> bytes:
        flowset_id = 0  # template flowset
        field_count = 2
        template_header = struct.pack(
            "!HH", self.TEMPLATE_ID, field_count
        )
        field_key = struct.pack("!HH", self.VENDOR_FIELD_TYPE, 64)
        field_data = struct.pack("!HH", self.VENDOR_FIELD_TYPE + 1, 1400)
        body = template_header + field_key + field_data
        length = 4 + len(body)
        return struct.pack("!HH", flowset_id, length) + body

    def _build_data_flowset(self, key: str, data: bytes) -> bytes:
        key_bytes = key.encode("utf-8")[:64].ljust(64, b"\x00")
        data_padded = data[:1400].ljust(1400, b"\x00")
        body = key_bytes + data_padded
        length = 4 + len(body)
        return struct.pack("!HH", self.TEMPLATE_ID, length) + body

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_key = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_key = uuid.uuid4().hex

        data = base64.b64encode(encoded_chunk.encode())

        def _send():
            import socket
            header = self._build_header(2)
            template_fs = self._build_template_flowset()
            data_fs = self._build_data_flowset(chunk_key, data)
            packet = header + template_fs + data_fs
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(packet, (self._host, self._port))
            finally:
                sock.close()

        try:
            await asyncio.to_thread(_send)
        except Exception:
            pass

        self._cache[chunk_key] = encoded_chunk
        return StoredChunkRef(adapter_name="netflow", location=chunk_key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"NetFlow record not cached: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}_"
        return [
            StoredChunkRef(adapter_name="netflow", location=k)
            for k in self._cache
            if k.startswith(prefix)
        ]

    async def health_check(self) -> bool:
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.close()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
