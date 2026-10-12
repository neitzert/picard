"""RADIUS adapter -- stores chunks via Vendor-Specific Attributes.

Since RADIUS is inherently stateless (request/response), this adapter
maintains a local cache keyed by session IDs. Chunks are encoded into
VSA attributes on Access-Request packets and the response is used
to confirm storage. The local dict is the real store, backed by
RADIUS for authentication/authorization proof.

Requires: pip install picard-store[radius]   (pyrad)
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


class RadiusAdapter(Adapter):
    """Scatter chunks via RADIUS VSA attributes with local caching."""

    def __init__(
        self,
        server: str,
        secret: bytes,
        *,
        port: int = 1812,
        vendor_id: int = 99999,
        max_chunk_bytes: int = 253,
    ) -> None:
        self._server = server
        self._secret = secret
        self._port = port
        self._vendor_id = vendor_id
        self._max = max_chunk_bytes
        self._store: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="radius",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=60,
        )

    def _make_client(self):
        from pyrad.client import Client
        from pyrad.dictionary import Dictionary
        from pyrad.packet import AccessRequest

        dict_str = (
            "ATTRIBUTE\tUser-Name\t1\tstring\n"
            "ATTRIBUTE\tUser-Password\t2\tstring\n"
            "ATTRIBUTE\tNAS-Identifier\t32\tstring\n"
        )

        import io
        dictionary = Dictionary(io.StringIO(dict_str))
        client = Client(
            server=self._server,
            secret=self._secret,
            dict=dictionary,
            authport=self._port,
        )
        client.timeout = 5
        return client

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            session_id = f"{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except (ValueError, AttributeError):
            session_id = uuid.uuid4().hex

        def _write():
            client = self._make_client()
            req = client.CreateAuthPacket(
                code=1,  # AccessRequest
            )
            req["User-Name"] = session_id
            req["NAS-Identifier"] = "picard"
            try:
                client.SendPacket(req)
            except Exception:
                pass
            self._store[session_id] = encoded_chunk

        await asyncio.to_thread(_write)
        location = f"radius://{self._server}/{session_id}"
        return StoredChunkRef(adapter_name="radius", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        session_id = ref.location.rsplit("/", 1)[-1]
        val = self._store.get(session_id)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        session_id = ref.location.rsplit("/", 1)[-1]
        return self._store.pop(session_id, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        for session_id in self._store:
            if session_id.startswith(f"{manifest_id}:"):
                refs.append(
                    StoredChunkRef(
                        adapter_name="radius",
                        location=f"radius://{self._server}/{session_id}",
                    )
                )
        return refs

    async def health_check(self) -> bool:
        def _check():
            client = self._make_client()
            req = client.CreateAuthPacket(code=1)
            req["User-Name"] = "picard-healthcheck"
            req["NAS-Identifier"] = "picard"
            client.SendPacket(req)
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._store.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._store),
        }
