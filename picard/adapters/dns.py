"""DNS adapter -- stores chunks as TXT records via RFC 2136 dynamic updates.

Each chunk becomes a TXT record under a configurable zone:
    c{chunk_index}.b{block_index}.{manifest_id}.{zone}

Requires: pip install picard-store[dns]
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class DnsAdapter(Adapter):
    """Scatter chunks into DNS TXT records."""

    def __init__(
        self,
        zone: str,
        nameserver: str,
        *,
        port: int = 53,
        ttl: int = 300,
        tsig_keyname: str | None = None,
        tsig_key: str | None = None,
        tsig_algorithm: str = "hmac-sha256",
        max_chunk_bytes: int = 200,
    ) -> None:
        self._zone = zone.rstrip(".")
        self._nameserver = nameserver
        self._port = port
        self._ttl = ttl
        self._tsig_keyname = tsig_keyname
        self._tsig_key = tsig_key
        self._tsig_algorithm = tsig_algorithm
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dns",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _fqdn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"c{chunk_index}.b{block_index}.{manifest_id}.{self._zone}"

    def _make_update(self):
        import dns.tsigkeyring
        import dns.update

        update = dns.update.Update(self._zone)
        if self._tsig_keyname and self._tsig_key:
            keyring = dns.tsigkeyring.from_text(
                {self._tsig_keyname: self._tsig_key}
            )
            update.use_tsig(keyring, algorithm=self._tsig_algorithm)
        return update

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import dns.query
        import dns.update

        chunk = Chunk.decode(encoded_chunk)
        fqdn = self._fqdn(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        name = f"c{chunk.chunk_index}.b{chunk.block_index}.{chunk.manifest_id}"

        def _write():
            update = self._make_update()
            update.replace(name, self._ttl, "TXT", f'"{encoded_chunk}"')
            dns.query.tcp(update, self._nameserver, port=self._port)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="dns", location=fqdn)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import dns.resolver

        def _read():
            answers = dns.resolver.resolve(ref.location, "TXT")
            parts = []
            for rdata in answers:
                for s in rdata.strings:
                    parts.append(s.decode("utf-8"))
            return "".join(parts)

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import dns.query

        relative = ref.location.removesuffix(f".{self._zone}").removesuffix(".")

        def _delete():
            update = self._make_update()
            update.delete(relative, "TXT")
            dns.query.tcp(update, self._nameserver, port=self._port)

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        """Attempt zone transfer (AXFR) to discover chunks for a manifest.

        Returns an empty list if AXFR is refused -- recovery without zone
        transfer access requires the local index.
        """
        import dns.query
        import dns.zone

        suffix = f".{manifest_id}.{self._zone}."

        def _list():
            try:
                z = dns.zone.from_xfr(
                    dns.query.xfr(self._nameserver, self._zone, port=self._port)
                )
            except Exception:
                return []
            refs: list[StoredChunkRef] = []
            for name, node in z.nodes.items():
                fqdn = f"{name}.{self._zone}"
                if fqdn.endswith(suffix.rstrip(".")):
                    refs.append(StoredChunkRef(adapter_name="dns", location=fqdn))
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        import dns.resolver

        try:
            await asyncio.to_thread(
                lambda: dns.resolver.resolve(self._zone, "SOA")
            )
            return True
        except Exception:
            return False
