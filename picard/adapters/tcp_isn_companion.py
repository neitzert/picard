"""TCP ISN companion -- captures SYN ISNs and serves them via Unix socket.

Listens for TCP SYN packets on a configurable port using scapy, extracts
the 32-bit ISN from each SYN, and indexes it by a token derived from the
source address and destination port. Exposes a line-protocol Unix socket
for the tcp_isn_adapter to store, retrieve, delete, and list tokens.

Socket protocol (one command per line, newline-terminated responses):
  STORE <token> <isn_hex>  -> OK
  GET <token>              -> <isn_hex> | MISS
  DEL <token>              -> OK
  LIST <prefix>            -> <token1>\\n...<tokenN>\\nEND
  QUIT                     -> BYE

Run: python3 tcp_isn_companion.py [--port PORT] [--socket PATH]

Requires: scapy>=2.5, CAP_NET_RAW
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading

try:
    from scapy.all import IP, TCP, sniff  # type: ignore[import]
    _SCAPY_OK = True
except ImportError:
    _SCAPY_OK = False

_store: dict[str, str] = {}
_lock = threading.Lock()


def _sniff_thread(port: int) -> None:
    if not _SCAPY_OK:
        return

    def _handler(pkt) -> None:
        if not (pkt.haslayer(IP) and pkt.haslayer(TCP)):
            return
        tcp = pkt[TCP]
        if not (tcp.flags & 0x02) or tcp.flags & 0x10:  # SYN but not SYN-ACK
            return
        if tcp.dport != port:
            return
        token = f"{pkt[IP].src}:{tcp.dport}:{tcp.seq}"
        isn_hex = hex(tcp.seq)
        with _lock:
            _store[token] = isn_hex

    sniff(
        filter=f"tcp and dst port {port} and tcp[tcpflags] & tcp-syn != 0",
        prn=_handler,
        store=False,
    )


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode().strip()
            if not cmd:
                continue

            parts = cmd.split(" ", 2)
            op = parts[0].upper()

            if op == "STORE" and len(parts) == 3:
                with _lock:
                    _store[parts[1]] = parts[2]
                writer.write(b"OK\n")

            elif op == "GET" and len(parts) == 2:
                with _lock:
                    val = _store.get(parts[1])
                writer.write((val + "\n").encode() if val else b"MISS\n")

            elif op == "DEL" and len(parts) == 2:
                with _lock:
                    _store.pop(parts[1], None)
                writer.write(b"OK\n")

            elif op == "LIST" and len(parts) == 2:
                prefix = parts[1]
                with _lock:
                    matches = [t for t in _store if t.startswith(prefix)]
                for t in matches:
                    writer.write((t + "\n").encode())
                writer.write(b"END\n")

            elif op == "QUIT":
                writer.write(b"BYE\n")
                break

            else:
                writer.write(b"ERR unknown command\n")

            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def _serve(sock_path: str) -> None:
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = await asyncio.start_unix_server(_handle_client, path=sock_path)
    os.chmod(sock_path, 0o600)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="TCP ISN companion")
    parser.add_argument("--port", type=int, default=54321)
    parser.add_argument("--socket", default="/tmp/picard_tcp_isn.sock")
    args = parser.parse_args()

    if not _SCAPY_OK:
        print("WARNING: scapy not available; SYN sniffing disabled", file=sys.stderr)
    else:
        t = threading.Thread(target=_sniff_thread, args=(args.port,), daemon=True)
        t.start()

    print(f"TCP ISN companion listening on {args.socket}", file=sys.stderr)
    asyncio.run(_serve(args.socket))


if __name__ == "__main__":
    main()
