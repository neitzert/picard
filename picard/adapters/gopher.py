"""Gopher adapter — stores chunks on a cooperating Gopher server.

Each chunk is stored as a text document accessible via a Gopher
selector.  The cooperating server understands PCHK-prefixed selectors
for write/read/delete/list operations.

Requires: a cooperating Gopher server (no pip dependency)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GopherAdapter(Adapter):
    """Scatter chunks into a Gopher server via custom selectors."""

    def __init__(
        self,
        host: str,
        port: int = 70,
        *,
        max_chunk_bytes: int = 50_000,
    ) -> None:
        self._host = host
        self._port = port
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gopher",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def _send_recv(self, selector: str) -> str:
        """Connect, send selector + CRLF, read until connection closes."""
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(f"{selector}\r\n".encode("utf-8"))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1_000_000), timeout=10.0)
            return data.decode("utf-8").rstrip(".\r\n")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        selector = f"PCHK-WRITE:{msg_id}\t{encoded_chunk}"
        await self._send_recv(selector)
        return StoredChunkRef(adapter_name="gopher", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        selector = f"PCHK-READ:{ref.location}"
        return await self._send_recv(selector)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        selector = f"PCHK-DELETE:{ref.location}"
        resp = await self._send_recv(selector)
        return resp.strip() == "OK"

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        selector = f"PCHK-LIST:{manifest_id}"
        resp = await self._send_recv(selector)
        if not resp.strip():
            return []
        refs: list[StoredChunkRef] = []
        for line in resp.strip().splitlines():
            line = line.strip()
            if line:
                refs.append(StoredChunkRef(adapter_name="gopher", location=line))
        return refs

    async def health_check(self) -> bool:
        try:
            await self._send_recv("")
            return True
        except Exception:
            return False
