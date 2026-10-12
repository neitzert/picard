"""Kerberos adapter -- stores chunks via GSSAPI-authenticated credential cache.

Chunks are written as files in a Kerberos-authenticated store directory,
with GSSAPI context used for authentication. The credential cache itself
serves as proof of authorization.

Requires: pip install picard-store[kerberos]   (gssapi)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

try:
    from picard.chunker import Chunk
except ImportError:  # pragma: no cover
    Chunk = None  # type: ignore[assignment,misc]


class KerberosAdapter(Adapter):
    """Scatter chunks into a Kerberos-authenticated credential store."""

    def __init__(
        self,
        realm: str,
        principal: str,
        *,
        keytab_path: str | Path | None = None,
        kdc_host: str | None = None,
        store_dir: str | Path | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._realm = realm
        self._principal = principal
        self._keytab_path = Path(keytab_path) if keytab_path else None
        self._kdc_host = kdc_host
        self._store_dir = Path(store_dir) if store_dir else Path.home() / ".picard" / "krb5_store"
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="kerberos",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            ttl_seconds=36000,
            rate_limit_per_min=30,
        )

    def _ensure_store(self) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)

    def _init_gssapi(self):
        import gssapi

        if self._keytab_path:
            os.environ["KRB5_KTNAME"] = str(self._keytab_path)
        name = gssapi.Name(
            self._principal, name_type=gssapi.NameType.kerberos_principal
        )
        creds = gssapi.Credentials(name=name, usage="initiate")
        return creds

    def _chunk_path(self, key: str) -> Path:
        hashed = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self._store_dir / f"{hashed}.pchk"

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, AttributeError):
            key = uuid.uuid4().hex

        def _write():
            self._init_gssapi()
            self._ensure_store()
            path = self._chunk_path(key)
            path.write_text(encoded_chunk, encoding="utf-8")

        await asyncio.to_thread(_write)
        location = f"krb5://{self._realm}/{key}"
        return StoredChunkRef(adapter_name="kerberos", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("/", 3)
        key_str = "/".join(key[3:]) if len(key) > 3 else key[-1]

        def _read():
            self._init_gssapi()
            path = self._chunk_path(key_str)
            if not path.exists():
                raise KeyError(f"chunk not found: {ref.location}")
            return path.read_text(encoding="utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("/", 3)
        key_str = "/".join(key[3:]) if len(key) > 3 else key[-1]

        def _delete():
            path = self._chunk_path(key_str)
            if path.exists():
                path.unlink()
                return True
            return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            self._ensure_store()
            refs: list[StoredChunkRef] = []
            for p in self._store_dir.glob("*.pchk"):
                refs.append(
                    StoredChunkRef(
                        adapter_name="kerberos",
                        location=f"krb5://{self._realm}/{p.stem}",
                    )
                )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            self._init_gssapi()
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _capacity():
            self._ensure_store()
            used = 0
            count = 0
            for p in self._store_dir.glob("*.pchk"):
                used += p.stat().st_size
                count += 1
            return {"used_bytes": used, "chunk_count": count}

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
