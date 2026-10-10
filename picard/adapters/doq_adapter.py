"""DNS-over-QUIC adapter — chunks as TXT records via DNS over QUIC (RFC 9250).

QUIC traffic looks like generic UDP, and the DNS content is encrypted,
giving this adapter strong transport-layer privacy.

Requires: pip install picard[dns,quic]  (aioquic, dnspython)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class DoqAdapter(Adapter):
    """Scatter chunks into DNS TXT records via DNS-over-QUIC."""

    def __init__(
        self,
        zone: str,
        resolver: str = "dns.adguard-dns.com",
        *,
        port: int = 853,
        ttl: int = 300,
        tsig_keyname: str | None = None,
        tsig_key: str | None = None,
        max_chunk_bytes: int = 200,
    ) -> None:
        self._zone = zone.rstrip(".")
        self._resolver = resolver
        self._port = port
        self._ttl = ttl
        self._tsig_keyname = tsig_keyname
        self._tsig_key = tsig_key
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dns-over-quic",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
        )

    def _fqdn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"c{chunk_index}.b{block_index}.{manifest_id}.{self._zone}"

    async def _quic_exchange(self, wire: bytes) -> bytes:
        from aioquic.asyncio import connect
        from aioquic.quic.configuration import QuicConfiguration

        config = QuicConfiguration(alpn_protocols=["doq"], is_client=True)
        config.verify_mode = False

        async with connect(self._resolver, self._port, configuration=config) as protocol:
            stream_id = protocol._quic.get_next_available_stream_id()
            protocol._quic.send_stream_data(stream_id, wire, end_stream=True)
            protocol.transmit()
            data = await protocol._queue.get()
            return data

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import dns.update

        try:
            chunk = Chunk.decode(encoded_chunk)
            fqdn = self._fqdn(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
            name = f"c{chunk.chunk_index}.b{chunk.block_index}.{chunk.manifest_id}"
        except ValueError:
            uid = uuid.uuid4().hex
            fqdn = f"{uid}.{self._zone}"
            name = uid

        update = dns.update.Update(self._zone)
        if self._tsig_keyname and self._tsig_key:
            import dns.tsigkeyring
            keyring = dns.tsigkeyring.from_text({self._tsig_keyname: self._tsig_key})
            update.use_tsig(keyring)
        update.replace(name, self._ttl, "TXT", f'"{encoded_chunk}"')
        wire = update.to_wire()
        await self._quic_exchange(wire)
        return StoredChunkRef(adapter_name="dns-over-quic", location=fqdn)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import dns.message
        import dns.rdatatype

        query = dns.message.make_query(ref.location, dns.rdatatype.TXT)
        wire = query.to_wire()
        resp_wire = await self._quic_exchange(wire)
        resp = dns.message.from_wire(resp_wire)
        parts = []
        for rrset in resp.answer:
            for rdata in rrset:
                for s in rdata.strings:
                    parts.append(s.decode("utf-8"))
        return "".join(parts)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import dns.update

        relative = ref.location.removesuffix(f".{self._zone}").removesuffix(".")
        update = dns.update.Update(self._zone)
        if self._tsig_keyname and self._tsig_key:
            import dns.tsigkeyring
            keyring = dns.tsigkeyring.from_text({self._tsig_keyname: self._tsig_key})
            update.use_tsig(keyring)
        update.delete(relative, "TXT")
        wire = update.to_wire()
        try:
            await self._quic_exchange(wire)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        import dns.message
        import dns.rdatatype

        try:
            query = dns.message.make_query(self._zone, dns.rdatatype.SOA)
            await self._quic_exchange(query.to_wire())
            return True
        except Exception:
            return False
