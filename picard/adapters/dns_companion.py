"""DNS companion -- authoritative DNS server + control socket for dns_question_adapter.

Serves two interfaces:
  1. UDP DNS on 0.0.0.0:PORT (default 5354) -- answers TXT queries for
     <token>.picard.local with stored data; accepts store/delete via
     crafted query names.
  2. Unix socket control channel at SOCK_PATH -- used by the adapter for
     faster store/get/del/list without DNS framing overhead.

Socket protocol (line-oriented, newline-terminated):
  STORE <token> <b64data>  -> OK
  GET <token>              -> <b64data> | MISS
  DEL <token>              -> OK
  LIST <prefix>            -> <token1>\\n...\\nEND
  QUIT                     -> BYE

Run: python3 dns_companion.py [--port PORT] [--socket PATH]

Requires: pip install dnslib
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys

try:
    import dnslib  # type: ignore[import]
    from dnslib import DNSRecord, DNSHeader, RR, QTYPE, TXT, A  # type: ignore[import]
    _DNSLIB_OK = True
except ImportError:
    _DNSLIB_OK = False

_DOMAIN_SUFFIX = ".picard.local."
_store: dict[str, str] = {}  # token -> b64data


# ---------------------------------------------------------------------------
# DNS UDP server
# ---------------------------------------------------------------------------

class _DnsProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self._transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not _DNSLIB_OK or self._transport is None:
            return
        try:
            req = DNSRecord.parse(data)
        except Exception:
            return

        qname = str(req.q.qname).lower()
        reply = req.reply()

        if qname.endswith(_DOMAIN_SUFFIX):
            label = qname[: -len(_DOMAIN_SUFFIX)]

            if label.startswith("store."):
                # store.<b64>.<token>.picard.local
                parts = label[len("store."):].split(".", 1)
                if len(parts) == 2:
                    b64data, token = parts
                    _store[token] = b64data

            elif label.startswith("del."):
                token = label[len("del."):]
                _store.pop(token, None)

            else:
                token = label
                if token in _store:
                    reply.add_answer(
                        RR(req.q.qname, QTYPE.TXT, rdata=TXT(_store[token]))
                    )

        self._transport.sendto(reply.pack(), addr)


# ---------------------------------------------------------------------------
# Unix socket control channel
# ---------------------------------------------------------------------------

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
                _store[parts[1]] = parts[2]
                writer.write(b"OK\n")

            elif op == "GET" and len(parts) == 2:
                val = _store.get(parts[1])
                writer.write((val + "\n").encode() if val else b"MISS\n")

            elif op == "DEL" and len(parts) == 2:
                _store.pop(parts[1], None)
                writer.write(b"OK\n")

            elif op == "LIST" and len(parts) == 2:
                prefix = parts[1]
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


async def _serve_unix(path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(_handle_client, path=path)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


async def _serve_dns(port: int) -> None:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        _DnsProtocol,
        local_addr=("0.0.0.0", port),
    )
    try:
        await asyncio.get_event_loop().create_future()
    finally:
        transport.close()


async def _main(port: int, sock_path: str) -> None:
    print(f"DNS companion: UDP :{port}, socket {sock_path}", file=sys.stderr)
    await asyncio.gather(
        _serve_unix(sock_path),
        _serve_dns(port),
        return_exceptions=True,
    )


def main() -> None:
    if not _DNSLIB_OK:
        print("WARNING: dnslib not installed; DNS UDP server disabled", file=sys.stderr)
        print("Install: pip install dnslib", file=sys.stderr)

    parser = argparse.ArgumentParser(description="DNS companion for picard")
    parser.add_argument("--port", type=int, default=5354)
    parser.add_argument("--socket", default="/tmp/picard_dns.sock")
    args = parser.parse_args()
    asyncio.run(_main(args.port, args.socket))


if __name__ == "__main__":
    main()
