"""DNS-over-HTTPS adapter — store chunks via DoH (RFC 8484).

Writes via DNS UPDATE encoded as application/dns-message over HTTPS.
Reads via DoH GET with base64url-encoded DNS queries.

Note: DoH traffic is encrypted HTTPS to well-known resolvers
(Cloudflare, Google), providing standard DNS transport privacy.

Requires: pip install picard[doh]  (aiohttp, dnspython)
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class DohAdapter(Adapter):
    """Scatter chunks via DNS-over-HTTPS queries."""

    def __init__(
        self,
        zone: str,
        *,
        resolver_url: str = "https://cloudflare-dns.com/dns-query",
        nameserver: str | None = None,
        nameserver_port: int = 53,
        max_chunk_bytes: int = 200,
    ) -> None:
        self._zone = zone.rstrip(".")
        self._resolver_url = resolver_url
        self._nameserver = nameserver
        self._ns_port = nameserver_port
        self._max = max_chunk_bytes
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dns-over-https",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
        )

    def _fqdn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"c{chunk_index}.b{block_index}.{manifest_id}.{self._zone}"

    def _chunk_key(self, encoded_chunk: str) -> tuple[str, int, int]:
        from picard.chunker import Chunk
        chunk = Chunk.decode(encoded_chunk)
        return chunk.manifest_id, chunk.block_index, chunk.chunk_index

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import dns.query
        import dns.update

        try:
            mid, bi, ci = self._chunk_key(encoded_chunk)
        except (ValueError, Exception):
            uid = uuid.uuid4().hex
            mid, bi, ci = uid, 0, 0

        fqdn = self._fqdn(mid, bi, ci)
        name = f"c{ci}.b{bi}.{mid}"

        if self._nameserver:
            def _write():
                update = dns.update.Update(self._zone)
                update.replace(name, 300, "TXT", f'"{encoded_chunk}"')
                dns.query.tcp(update, self._nameserver, port=self._ns_port)

            await asyncio.to_thread(_write)
        else:
            import dns.message
            import dns.rdatatype

            update = dns.update.Update(self._zone)
            update.replace(name, 300, "TXT", f'"{encoded_chunk}"')
            wire = update.to_wire()

            session = await self._get_session()
            async with session.post(
                self._resolver_url,
                data=wire,
                headers={
                    "Content-Type": "application/dns-message",
                    "Accept": "application/dns-message",
                },
            ) as resp:
                resp.raise_for_status()

        loc = f"doh://{self._resolver_url}/{fqdn}"
        return StoredChunkRef(adapter_name="dns-over-https", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import dns.message
        import dns.rdatatype

        parts = ref.location.replace("doh://", "").split("/", 1)
        fqdn = parts[1] if len(parts) > 1 else parts[0]

        query = dns.message.make_query(fqdn, dns.rdatatype.TXT)
        wire = query.to_wire()
        b64 = base64.urlsafe_b64encode(wire).rstrip(b"=").decode()

        session = await self._get_session()
        async with session.get(
            self._resolver_url,
            params={"dns": b64},
            headers={"Accept": "application/dns-message"},
        ) as resp:
            resp.raise_for_status()
            response_wire = await resp.read()

        response = dns.message.from_wire(response_wire)
        for rrset in response.answer:
            for rdata in rrset:
                for s in rdata.strings:
                    return s.decode("utf-8")

        raise KeyError(f"no TXT record found: {fqdn}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        parts = ref.location.replace("doh://", "").split("/", 1)
        fqdn = parts[1] if len(parts) > 1 else parts[0]
        relative = fqdn.removesuffix(f".{self._zone}").removesuffix(".")

        if self._nameserver:
            import dns.query
            import dns.update

            def _delete():
                update = dns.update.Update(self._zone)
                update.delete(relative, "TXT")
                dns.query.tcp(update, self._nameserver, port=self._ns_port)

            try:
                await asyncio.to_thread(_delete)
                return True
            except Exception:
                return False
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import dns.message
        import dns.rdatatype

        try:
            query = dns.message.make_query(self._zone, dns.rdatatype.SOA)
            wire = query.to_wire()
            b64 = base64.urlsafe_b64encode(wire).rstrip(b"=").decode()

            session = await self._get_session()
            async with session.get(
                self._resolver_url,
                params={"dns": b64},
                headers={"Accept": "application/dns-message"},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
