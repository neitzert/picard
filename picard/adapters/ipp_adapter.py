"""IPP adapter — stores chunks as Internet Printing Protocol print job attributes.

Stores chunk data in job-name, document-name, or job-originating-user-name
attributes of print jobs submitted via IPP (RFC 8011).  The data lives in
the print queue until the job is purged.

Requires: network access to an IPP printer on port 631
"""

from __future__ import annotations

import struct
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class IppAdapter(Adapter):
    """Scatter chunks into IPP print job attributes."""

    def __init__(
        self,
        printer_url: str,
        *,
        username: str | None = None,
        max_chunk_bytes: int = 1024,
    ) -> None:
        self._printer_url = printer_url
        self._username = username or "picard"
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._request_id = 1
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
            name="ipp",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=30,
        )

    def _build_ipp_request(self, operation: int, attributes: dict) -> bytes:
        buf = bytearray()
        buf += struct.pack(">BBH", 2, 0, operation)
        buf += struct.pack(">I", self._request_id)
        self._request_id += 1
        buf += b"\x01"  # operation-attributes-tag
        for name, value in attributes.items():
            name_bytes = name.encode("utf-8")
            value_bytes = value.encode("utf-8")
            buf += struct.pack(">BH", 0x41, len(name_bytes))
            buf += name_bytes
            buf += struct.pack(">H", len(value_bytes))
            buf += value_bytes
        buf += b"\x03"  # end-of-attributes-tag
        return bytes(buf)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import asyncio
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        ipp_data = self._build_ipp_request(
            0x0002,  # Print-Job
            {
                "attributes-charset": "utf-8",
                "attributes-natural-language": "en",
                "printer-uri": self._printer_url,
                "requesting-user-name": self._username,
                "job-name": encoded_chunk[:self._max],
                "document-name": f"picard-{chunk_id}",
            },
        )

        http_url = self._printer_url.replace("ipp://", "http://").replace(
            "ipps://", "https://"
        )

        session = await self._get_session()
        async with session.post(
            http_url,
            data=ipp_data,
            headers={"Content-Type": "application/ipp"},
        ) as resp:
            response_data = await resp.read()

        location = f"ipp://{self._printer_url}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="ipp", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        val = self._cache.get(ref.location)
        if val is None:
            raise KeyError(f"chunk not found: {ref.location}")
        return val

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ipp", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            http_url = self._printer_url.replace("ipp://", "http://").replace(
                "ipps://", "https://"
            )
            ipp_data = self._build_ipp_request(
                0x000B,  # Get-Printer-Attributes
                {
                    "attributes-charset": "utf-8",
                    "attributes-natural-language": "en",
                    "printer-uri": self._printer_url,
                },
            )
            session = await self._get_session()
            async with session.post(
                http_url,
                data=ipp_data,
                headers={"Content-Type": "application/ipp"},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
