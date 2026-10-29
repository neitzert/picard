"""LDAP attribute adapter -- stores chunks as LDAP directory entry attributes.

Each write_chunk creates (or updates) an inetOrgPerson entry under the base DN
with the DN: cn={token},ou=picard,{base_dn}. The encoded chunk is stored in the
description attribute. Cold reads perform an LDAP search by cn={token}.

Requires: pip install ldap3
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ldap-attribute"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class LdapAttributeAdapter(Adapter):

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 389,
        bind_dn: str = "",
        bind_password: str = "",
        base_dn: str = "dc=example,dc=com",
        use_ssl: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._bind_dn = bind_dn
        self._bind_password = bind_password
        self._base_dn = base_dn
        self._ou_dn = f"ou=picard,{base_dn}"
        self._use_ssl = use_ssl
        self._reg: dict[str, str] = {}

    def _connect(self):
        import ldap3
        server = ldap3.Server(self._host, port=self._port, use_ssl=self._use_ssl)
        conn = ldap3.Connection(server, user=self._bind_dn,
                                password=self._bind_password,
                                auto_bind=ldap3.AUTO_BIND_TLS_BEFORE_BIND
                                if not self._use_ssl else ldap3.AUTO_BIND_NO_TLS)
        return conn

    def _ensure_ou(self, conn) -> None:
        import ldap3
        conn.add(self._ou_dn, object_class=["organizationalUnit"],
                 attributes={"ou": "picard"})
        # Ignore error if already exists

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            tags={"requires": "ldap3", "substrate": "ldap-entry-attribute"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import ldap3

        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        dn = f"cn={token},{self._ou_dn}"

        def _do_write():
            conn = self._connect()
            self._ensure_ou(conn)
            attrs = {
                "cn": token,
                "sn": "picard",
                "description": encoded_chunk,
                "displayName": f"picard:{manifest_id}",
            }
            conn.add(dn, object_class=["inetOrgPerson"], attributes=attrs)
            conn.unbind()

        await asyncio.to_thread(_do_write)
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "dn": dn,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import ldap3

        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]

        dn = loc["dn"]

        def _do_read():
            conn = self._connect()
            conn.search(dn, "(objectClass=inetOrgPerson)",
                        attributes=["description"])
            entries = conn.entries
            conn.unbind()
            return entries

        entries = await asyncio.to_thread(_do_read)
        if not entries:
            raise KeyError(f"ldap-attribute chunk not found: {token}")
        chunk = str(entries[0].description)
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        dn = loc.get("dn", f"cn={token},{self._ou_dn}")
        self._reg.pop(token, None)

        def _do_delete():
            conn = self._connect()
            conn.delete(dn)
            conn.unbind()

        try:
            await asyncio.to_thread(_do_delete)
        except Exception:
            pass
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import ldap3

        def _do_list():
            conn = self._connect()
            conn.search(
                self._ou_dn,
                f"(&(objectClass=inetOrgPerson)(displayName=picard:{manifest_id}))",
                attributes=["cn", "description"],
            )
            entries = list(conn.entries)
            conn.unbind()
            return entries

        refs: list[StoredChunkRef] = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token,
                                         "dn": f"cn={token},{self._ou_dn}",
                                         "manifest": manifest_id}),
                ))

        try:
            entries = await asyncio.to_thread(_do_list)
            tokens_seen = {json.loads(r.location)["token"] for r in refs}
            for entry in entries:
                tok = str(entry.cn)
                if tok not in tokens_seen:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps({"token": tok,
                                             "dn": f"cn={tok},{self._ou_dn}",
                                             "manifest": manifest_id}),
                    ))
        except Exception:
            pass
        return refs

    async def health_check(self) -> bool:
        if not self._bind_dn:
            return False
        try:
            def _ping():
                conn = self._connect()
                result = conn.search(self._base_dn, "(objectClass=*)",
                                     search_scope="BASE", attributes=[])
                conn.unbind()
                return result
            return await asyncio.to_thread(_ping)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
