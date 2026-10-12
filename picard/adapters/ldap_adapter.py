"""LDAP adapter -- stores chunks as attributes on directory objects.

Each chunk becomes an organizationalUnit with the encoded data in
the ``description`` attribute:

    ou=c{chunk}.b{block}.{manifest_id},{base_dn}

Requires: pip install picard-store[ldap]   (ldap3)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

try:
    from picard.chunker import Chunk
except ImportError:  # pragma: no cover
    Chunk = None  # type: ignore[assignment,misc]


class LdapAdapter(Adapter):
    """Scatter chunks into LDAP directory entries."""

    def __init__(
        self,
        host: str,
        base_dn: str,
        bind_dn: str,
        bind_password: str,
        *,
        port: int = 389,
        use_tls: bool = False,
        max_chunk_bytes: int = 63_000,
    ) -> None:
        self._host = host
        self._base_dn = base_dn
        self._bind_dn = bind_dn
        self._bind_password = bind_password
        self._port = port
        self._use_tls = use_tls
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ldap",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=600,
        )

    def _connect(self):
        import ldap3

        server = ldap3.Server(self._host, port=self._port, use_ssl=self._use_tls)
        conn = ldap3.Connection(
            server, self._bind_dn, self._bind_password, auto_bind=True
        )
        return conn

    def _chunk_dn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"ou=c{chunk_index}.b{block_index}.{manifest_id},{self._base_dn}"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            dn = self._chunk_dn(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except (ValueError, AttributeError):
            dn = f"ou={uuid.uuid4().hex},{self._base_dn}"

        def _write():
            conn = self._connect()
            try:
                conn.add(
                    dn,
                    object_class=["organizationalUnit"],
                    attributes={"description": encoded_chunk},
                )
            finally:
                conn.unbind()

        await asyncio.to_thread(_write)
        location = f"ldap://{self._host}/{dn}"
        return StoredChunkRef(adapter_name="ldap", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        dn = ref.location.split("/", 3)[-1]

        def _read():
            conn = self._connect()
            try:
                conn.search(dn, "(objectClass=*)", attributes=["description"])
                if not conn.entries:
                    raise KeyError(f"chunk not found: {ref.location}")
                return str(conn.entries[0].description)
            finally:
                conn.unbind()

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        dn = ref.location.split("/", 3)[-1]

        def _delete():
            conn = self._connect()
            try:
                conn.delete(dn)
                return conn.result["result"] == 0
            finally:
                conn.unbind()

        try:
            return await asyncio.to_thread(_delete)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            conn = self._connect()
            try:
                search_filter = f"(ou=*{manifest_id}*)"
                conn.search(
                    self._base_dn,
                    search_filter,
                    search_scope="LEVEL",
                    attributes=["ou"],
                )
                refs: list[StoredChunkRef] = []
                for entry in conn.entries:
                    location = f"ldap://{self._host}/{entry.entry_dn}"
                    refs.append(StoredChunkRef(adapter_name="ldap", location=location))
                return refs
            finally:
                conn.unbind()

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            conn = self._connect()
            conn.unbind()
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _capacity():
            conn = self._connect()
            try:
                search_filter = "(objectClass=organizationalUnit)"
                conn.search(
                    self._base_dn,
                    search_filter,
                    search_scope="LEVEL",
                    attributes=["description"],
                )
                used = 0
                count = 0
                for entry in conn.entries:
                    desc = str(entry.description) if entry.description else ""
                    used += len(desc.encode())
                    count += 1
                return {"used_bytes": used, "chunk_count": count}
            finally:
                conn.unbind()

        try:
            result = await asyncio.to_thread(_capacity)
            return {
                "used_bytes": result["used_bytes"],
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": result["chunk_count"],
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
