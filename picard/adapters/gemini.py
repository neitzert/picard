"""Gemini protocol adapter — stores chunks on a cooperating Gemini server.

Each chunk is stored as a Gemini text document.  The cooperating server
understands PICARD paths under /picard/ for write/read/delete/list
operations.  Communication is TLS-wrapped TCP per the Gemini spec.

Requires: a cooperating Gemini server (no pip dependency)
"""

from __future__ import annotations

import asyncio
import ssl
import uuid
from typing import Sequence
from urllib.parse import quote

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GeminiAdapter(Adapter):
    """Scatter chunks into a Gemini server via TLS-wrapped requests."""

    def __init__(
        self,
        host: str,
        port: int = 1965,
        *,
        client_cert: str | None = None,
        ca_file: str | None = None,
        max_chunk_bytes: int = 50_000,
    ) -> None:
        self._host = host
        self._port = port
        self._client_cert = client_cert
        self._ca_file = ca_file
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="gemini",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _make_ssl_context(self) -> ssl.SSLContext:
        # Gemini uses TOFU (Trust On First Use); pass ca_file to pin a cert/CA instead
        if self._ca_file:
            ctx = ssl.create_default_context(cafile=self._ca_file)
        else:
            ctx = ssl.create_default_context()
            # TOFU mode: most Gemini servers present self-signed certs by design
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self._client_cert:
            ctx.load_cert_chain(self._client_cert)
        return ctx

    async def _send_recv(self, url: str) -> tuple[int, str, str]:
        """Send a Gemini request and parse the response.

        Returns (status_code, meta, body).
        """
        ctx = self._make_ssl_context()
        reader, writer = await asyncio.open_connection(
            self._host, self._port, ssl=ctx,
        )
        try:
            writer.write(f"{url}\r\n".encode("utf-8"))
            await writer.drain()

            header_line = await asyncio.wait_for(
                reader.readline(), timeout=10.0,
            )
            header = header_line.decode("utf-8").strip()
            status = int(header[:2])
            meta = header[3:] if len(header) > 2 else ""

            body = ""
            if 20 <= status < 30:
                raw = await asyncio.wait_for(reader.read(1_000_000), timeout=10.0)
                body = raw.decode("utf-8")

            return status, meta, body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        data = quote(encoded_chunk, safe="")
        url = f"gemini://{self._host}/picard/write/{msg_id}?data={data}"
        await self._send_recv(url)
        return StoredChunkRef(adapter_name="gemini", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        url = f"gemini://{self._host}/picard/read/{ref.location}"
        status, meta, body = await self._send_recv(url)
        if status != 20:
            raise ValueError(f"Gemini read failed: status {status} {meta}")
        return body

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        url = f"gemini://{self._host}/picard/delete/{ref.location}"
        try:
            status, _, _ = await self._send_recv(url)
            return status == 20
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        url = f"gemini://{self._host}/picard/list/{manifest_id}"
        status, _, body = await self._send_recv(url)
        if status != 20 or not body.strip():
            return []
        refs: list[StoredChunkRef] = []
        for line in body.strip().splitlines():
            line = line.strip()
            if line:
                refs.append(StoredChunkRef(adapter_name="gemini", location=line))
        return refs

    async def health_check(self) -> bool:
        try:
            status, _, _ = await self._send_recv(f"gemini://{self._host}/")
            return status == 20
        except Exception:
            return False
