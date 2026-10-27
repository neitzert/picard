"""GPU VRAM companion daemon -- holds OpenCL buffers alive and serves a Unix socket.

Run: python3 gpu_vram_companion.py [--socket /tmp/picard_gpu.sock]

Protocol (line-oriented, UTF-8):
  STORE <token> <base64_data>  ->  OK  or  ERR <msg>
  GET <token>                  ->  <base64_data>  or  MISS
  DEL <token>                  ->  OK
  LIST                         ->  <token>\n...\nEND
  HEALTH                       ->  OK
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys


def _select_device():
    try:
        import pyopencl as cl
        platforms = cl.get_platforms()
        for plat in platforms:
            for dev in plat.get_devices(cl.device_type.GPU):
                return plat, dev
        for plat in platforms:
            for dev in plat.get_devices(cl.device_type.CPU):
                print(f"[gpu_vram] no GPU found, falling back to CPU: {dev.name}", file=sys.stderr)
                return plat, dev
        raise RuntimeError("no OpenCL devices found")
    except ImportError:
        raise RuntimeError("pyopencl not installed: pip install pyopencl")


class GpuStore:
    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._buffers: dict[str, bytes] = {}
        self._ctx = None
        self._queue = None
        self._cl = None

    def _ensure_cl(self) -> None:
        if self._ctx is not None:
            return
        import pyopencl as cl
        self._cl = cl
        plat, dev = _select_device()
        print(f"[gpu_vram] using device: {dev.name}", file=sys.stderr)
        self._ctx = cl.Context([dev])
        self._queue = cl.CommandQueue(self._ctx)

    def store(self, token: str, data: bytes) -> None:
        try:
            self._ensure_cl()
            import pyopencl as cl
            buf = cl.Buffer(self._ctx, cl.mem_flags.READ_WRITE | cl.mem_flags.COPY_HOST_PTR, hostbuf=data)
            self._buffers[token] = buf
        except Exception:
            self._buffers[token] = data

    def get(self, token: str) -> bytes | None:
        val = self._buffers.get(token)
        if val is None:
            return None
        try:
            import pyopencl as cl
            import numpy as np
            if isinstance(val, cl.Buffer):
                out = bytearray(val.size)
                cl.enqueue_copy(self._queue, out, val)
                self._queue.finish()
                return bytes(out)
        except Exception:
            pass
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
        return None

    def delete(self, token: str) -> None:
        self._buffers.pop(token, None)

    def list_tokens(self) -> list[str]:
        return list(self._buffers.keys())


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, store: GpuStore) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.decode().rstrip("\n")
            parts = line.split(" ", 2)
            cmd = parts[0].upper()

            if cmd == "HEALTH":
                writer.write(b"OK\n")
            elif cmd == "LIST":
                for tok in store.list_tokens():
                    writer.write(f"{tok}\n".encode())
                writer.write(b"END\n")
            elif cmd == "STORE" and len(parts) == 3:
                token, b64 = parts[1], parts[2]
                try:
                    data = base64.b64decode(b64)
                    store.store(token, data)
                    writer.write(b"OK\n")
                except Exception as e:
                    writer.write(f"ERR {e}\n".encode())
            elif cmd == "GET" and len(parts) >= 2:
                token = parts[1]
                data = store.get(token)
                if data is None:
                    writer.write(b"MISS\n")
                else:
                    writer.write(base64.b64encode(data) + b"\n")
            elif cmd == "DEL" and len(parts) >= 2:
                store.delete(parts[1])
                writer.write(b"OK\n")
            else:
                writer.write(b"ERR unknown command\n")
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def main(socket_path: str) -> None:
    store = GpuStore(socket_path)
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, store),
        path=socket_path,
    )
    print(f"[gpu_vram] listening on {socket_path}", file=sys.stderr)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/picard_gpu.sock")
    args = ap.parse_args()
    asyncio.run(main(args.socket))
