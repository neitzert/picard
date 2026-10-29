"""DNS TXT record adapter -- stores chunks as DNS TXT records via dynamic DNS UPDATE.

Each write_chunk sends a DNS UPDATE (RFC 2136) to add a TXT record under the
zone. The record name is {token}.picard.{zone}. Cold reads resolve the name
using standard DNS queries.

Requires: pip install dnspython
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "dns-txt-record"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class DnsTxtRecordAdapter(Adapter):

    def __init__(
        self,
        *,
        nameserver: str = "127.0.0.1",
        zone: str = "picard.local",
        tsig_keyname: str = "",
        tsig_secret: str = "",
        ttl: int = 300,
    ) -> None:
        self._nameserver = nameserver
        self._zone = zone
        self._tsig_keyname = tsig_keyname
        self._tsig_secret = tsig_secret
        self._ttl = ttl
        self._reg: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65535,
            persistent=True,
            ordered=False,
            ttl_seconds=self._ttl,
            rate_limit_per_min=100,
            tags={"requires": "dnspython", "substrate": "dns-txt-record"},
        )

    def _build_update(self, name: str, action: str, encoded_chunk: str = "") -> "dns.update.Update":
        import dns.update
        import dns.tsigkeyring
        update = dns.update.Update(self._zone)
        if self._tsig_keyname and self._tsig_secret:
            import base64
            keyring = dns.tsigkeyring.from_text({self._tsig_keyname: self._tsig_secret})
            update.use_tsig(keyring=keyring, keyname=self._tsig_keyname)
        if action == "add":
            update.add(name, self._ttl, "TXT", encoded_chunk)
        elif action == "delete":
            update.delete(name, "TXT")
        return update

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import dns.query

        token = uuid.uuid4().hex[:12]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        name = f"{token}.picard"

        update = self._build_update(name, "add", encoded_chunk)
        await asyncio.to_thread(dns.query.udp, update, self._nameserver)

        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token,
                                 "name": f"{name}.{self._zone}",
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import dns.resolver

        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        fqdn = loc["name"]
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [self._nameserver]
        answers = await asyncio.to_thread(resolver.resolve, fqdn, "TXT")
        for rdata in answers:
            for string in rdata.strings:
                chunk = string.decode()
                self._reg[token] = chunk
                return chunk
        raise KeyError(f"dns-txt-record chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        import dns.query

        loc = json.loads(ref.location)
        token = loc["token"]
        self._reg.pop(token, None)

        name = f"{token}.picard"
        try:
            update = self._build_update(name, "delete")
            await asyncio.to_thread(dns.query.udp, update, self._nameserver)
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "name": f"{token}.picard.{self._zone}",
                                         "manifest": manifest_id}),
                ))
        return refs

    async def health_check(self) -> bool:
        try:
            import dns.resolver
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [self._nameserver]
            await asyncio.to_thread(resolver.resolve, self._zone, "SOA")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
