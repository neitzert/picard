"""IPv4 ID companion daemon.

Listens on a Unix socket for STORE/GET/DEL/LIST commands from Ipv4IdAdapter.
Optionally sniffs loopback with scapy to record IP.id sequences.

Usage:
    python -m picard.adapters.ipv4_id_companion [--socket /tmp/picard_ipv4_id.sock]
"""

from __future__ import annotations

import asyncio
import base64
import os
import signal

_SOCK_PATH = "/tmp/picard_ipv4_id.sock"
_store: dict[str, bytes] = {}


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode().strip()
            if not cmd:
                continue
            parts = cmd.split(" ", 2)
            verb = parts[0].upper()

            if verb == "STORE" and len(parts) == 3:
                token, b64 = parts[1], parts[2]
                try:
                    data = base64.b64decode(b64)
                except Exception:
                    writer.write(b"ERR bad base64\n")
                    await writer.drain()
                    continue
                _store[token] = data
                writer.write(b"OK\n")

            elif verb == "GET" and len(parts) == 2:
                data = _store.get(parts[1])
                if data is None:
                    writer.write(b"NOTFOUND\n")
                else:
                    writer.write(f"DATA {base64.b64encode(data).decode()}\n".encode())

            elif verb == "DEL" and len(parts) == 2:
                _store.pop(parts[1], None)
                writer.write(b"OK\n")

            elif verb == "LIST" and len(parts) == 2:
                manifest_id = parts[1]
                tokens = []
                for tok, data in _store.items():
                    try:
                        from picard.chunker import Chunk
                        if Chunk.decode(data.decode()).manifest_id == manifest_id:
                            tokens.append(tok)
                    except Exception:
                        continue
                writer.write(f"TOKENS {','.join(tokens)}\n".encode())

            else:
                writer.write(b"ERR unknown command\n")

            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def main(sock_path: str = _SOCK_PATH) -> None:
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    server = await asyncio.start_unix_server(_handle, path=sock_path)
    os.chmod(sock_path, 0o600)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, server.close)

    async with server:
        await server.serve_forever()

    if os.path.exists(sock_path):
        os.unlink(sock_path)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--socket", default=_SOCK_PATH)
    args = p.parse_args()
    asyncio.run(main(args.socket))
