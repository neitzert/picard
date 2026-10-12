"""DNS-over-TLS adapter — chunks as TXT records via encrypted DNS (port 853).

Queries and updates travel inside a TLS tunnel, so the ISP can see
that DNS-over-TLS is happening but cannot inspect the query content.

Requires: pip install picard-store[dns]
"""

from __future__ import annotations

import asyncio
import ssl
import struct
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class DotAdapter(Adapter):
    """Scatter chunks into DNS TXT records over DNS-over-TLS."""

    def __init__(
        self,
        zone: str,
        resolver: str = "1.1.1.1",
        *,
        port: int = 853,
        ttl: int = 300,
        tsig_keyname: str | None = None,
        tsig_key: str | None = None,
        max_chunk_bytes: int = 200,
        ca_file: str | None = None,
    ) -> None:
        self._zone = zone.rstrip(".")
        self._resolver = resolver
        self._port = port
        self._ttl = ttl
        self._tsig_keyname = tsig_keyname
        self._tsig_key = tsig_key
        self._max = max_chunk_bytes
        self._ca_file = ca_file

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dns-over-tls",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
        )

    def _fqdn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"c{chunk_index}.b{block_index}.{manifest_id}.{self._zone}"

    def _tls_context(self) -> ssl.SSLContext:
        if self._ca_file:
            ctx = ssl.create_default_context(cafile=self._ca_file)
        else:
            ctx = ssl.create_default_context()
        return ctx

    def _send_over_tls(self, wire: bytes) -> bytes:
        import socket as _socket

        ctx = self._tls_context()
        with _socket.create_connection((self._resolver, self._port), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=self._resolver) as sock:
                length_prefix = struct.pack("!H", len(wire))
                sock.sendall(length_prefix + wire)
                resp_len_raw = sock.recv(2)
                if len(resp_len_raw) < 2:
                    raise ConnectionError("short TLS DNS response")
                resp_len = struct.unpack("!H", resp_len_raw)[0]
                resp = b""
                while len(resp) < resp_len:
                    resp += sock.recv(resp_len - len(resp))
                return resp

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            fqdn = self._fqdn(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
            name = f"c{chunk.chunk_index}.b{chunk.block_index}.{chunk.manifest_id}"
        except ValueError:
            import uuid
            uid = uuid.uuid4().hex
            fqdn = f"{uid}.{self._zone}"
            name = uid

        def _write():
            import dns.update
            update = dns.update.Update(self._zone)
            if self._tsig_keyname and self._tsig_key:
                import dns.tsigkeyring
                keyring = dns.tsigkeyring.from_text({self._tsig_keyname: self._tsig_key})
                update.use_tsig(keyring)
            update.replace(name, self._ttl, "TXT", f'"{encoded_chunk}"')
            wire = update.to_wire()
            self._send_over_tls(wire)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="dns-over-tls", location=fqdn)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import dns.message
        import dns.rdatatype

        def _read():
            query = dns.message.make_query(ref.location, dns.rdatatype.TXT)
            wire = query.to_wire()
            resp_wire = self._send_over_tls(wire)
            resp = dns.message.from_wire(resp_wire)
            parts = []
            for rrset in resp.answer:
                for rdata in rrset:
                    for s in rdata.strings:
                        parts.append(s.decode("utf-8"))
            return "".join(parts)

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        relative = ref.location.removesuffix(f".{self._zone}").removesuffix(".")

        def _delete():
            import dns.update
            update = dns.update.Update(self._zone)
            if self._tsig_keyname and self._tsig_key:
                import dns.tsigkeyring
                keyring = dns.tsigkeyring.from_text({self._tsig_keyname: self._tsig_key})
                update.use_tsig(keyring)
            update.delete(relative, "TXT")
            wire = update.to_wire()
            self._send_over_tls(wire)

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import dns.message
        import dns.rdatatype

        def _check():
            query = dns.message.make_query(self._zone, dns.rdatatype.SOA)
            wire = query.to_wire()
            self._send_over_tls(wire)

        try:
            await asyncio.to_thread(_check)
            return True
        except Exception:
            return False
