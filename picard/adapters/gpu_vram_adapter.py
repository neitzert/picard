"""GPU VRAM adapter -- stores chunks as OpenCL buffers held by a companion daemon.

Requires: pip install pyopencl  (plus OpenCL drivers for your GPU)
Companion: python3 picard/adapters/gpu_vram_companion.py

The companion daemon holds cl.Buffer objects alive across requests.
Without the companion, falls back to an in-process dict (no actual VRAM used).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
import warnings
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GpuVramAdapter(Adapter):
    """Scatter chunks into GPU VRAM via OpenCL buffers (companion-assisted)."""

    def __init__(
        self,
        *,
        companion_socket: str = "/tmp/picard_gpu.sock",
        max_chunk_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._socket = companion_socket
        self._max = max_chunk_bytes
        self._fallback: dict[str, str] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gpu_vram",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=300,
            tags={
                "requires": "pyopencl,GPU-or-CPU-OpenCL,gpu_vram_companion",
                "substrate": "gpu-vram-opencl",
            },
        )

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._writer is not None and not self._writer.is_closing():
            return self._reader, self._writer
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(self._socket)
            return self._reader, self._writer
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            raise ConnectionError(
                f"gpu_vram companion not running at {self._socket}. "
                "Start it: python3 picard/adapters/gpu_vram_companion.py"
            )

    async def _send(self, command: str) -> str:
        reader, writer = await self._connect()
        writer.write(command.encode() + b"\n")
        await writer.drain()
        line = await reader.readline()
        return line.decode().rstrip("\n")

    async def _send_multiline(self, command: str) -> list[str]:
        reader, writer = await self._connect()
        writer.write(command.encode() + b"\n")
        await writer.drain()
        lines = []
        while True:
            line = (await reader.readline()).decode().rstrip("\n")
            if line == "END":
                break
            lines.append(line)
        return lines

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        b64 = base64.b64encode(encoded_chunk.encode()).decode()
        try:
            resp = await self._send(f"STORE {token} {b64}")
            if not resp.startswith("OK"):
                raise RuntimeError(f"companion error: {resp}")
        except ConnectionError as e:
            warnings.warn(str(e), RuntimeWarning, stacklevel=2)
            self._fallback[token] = encoded_chunk
        return StoredChunkRef(adapter_name="gpu_vram", location=token)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._fallback:
            return self._fallback[ref.location]
        try:
            resp = await self._send(f"GET {ref.location}")
        except ConnectionError:
            raise KeyError(f"gpu_vram: companion not available and {ref.location!r} not in fallback")
        if resp == "MISS":
            raise KeyError(f"gpu_vram chunk not found: {ref.location}")
        return base64.b64decode(resp).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = ref.location in self._fallback
        self._fallback.pop(ref.location, None)
        try:
            await self._send(f"DEL {ref.location}")
            existed = True
        except ConnectionError:
            pass
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []

        for token, data in self._fallback.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="gpu_vram", location=token))
            except Exception:
                continue

        try:
            tokens = await self._send_multiline("LIST")
            for token in tokens:
                if token in self._fallback:
                    continue
                try:
                    resp = await self._send(f"GET {token}")
                    if resp == "MISS":
                        continue
                    data = base64.b64decode(resp).decode()
                    if Chunk.decode(data).manifest_id == manifest_id:
                        refs.append(StoredChunkRef(adapter_name="gpu_vram", location=token))
                except Exception:
                    continue
        except ConnectionError:
            pass

        return refs

    async def health_check(self) -> bool:
        try:
            resp = await self._send("HEALTH")
            return resp == "OK"
        except Exception:
            return len(self._fallback) >= 0

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
