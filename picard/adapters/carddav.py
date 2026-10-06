"""CardDAV adapter — stores chunks as vCard NOTE fields in an address book.

Each chunk becomes a contact entry with the PCHK-encoded string in the
NOTE property.  The contact's FN (formatted name) carries the manifest
tag for discovery via addressbook-query REPORT.

Requires: pip install httpx
"""

from __future__ import annotations

import re
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_NOTE_RE = re.compile(r"^NOTE:(.+?)\r?$", re.MULTILINE)
_UID_RE = re.compile(r"^UID:([^@]+)@picard\r?$", re.MULTILINE)


def _build_vcard(msg_id: str, manifest_id: str, block: int, chunk: int, data: str) -> str:
    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"UID:{msg_id}@picard\r\n"
        f"FN:PCHK {manifest_id} b{block} c{chunk}\r\n"
        f"NOTE:{data}\r\n"
        "END:VCARD\r\n"
    )


def _extract_note(vcard_text: str) -> str | None:
    m = _NOTE_RE.search(vcard_text)
    return m.group(1) if m else None


def _extract_uid(vcard_text: str) -> str | None:
    m = _UID_RE.search(vcard_text)
    return m.group(1) if m else None


class CarddavAdapter(Adapter):
    """Scatter chunks into vCard contacts on a CardDAV server."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        addressbook_path: str = "addressbooks/picard/",
        *,
        max_chunk_bytes: int = 50_000,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._ab_path = addressbook_path.strip("/") + "/"
        self._max = max_chunk_bytes
        self._client: object | None = None

    @property
    def _ab_url(self) -> str:
        return f"{self._url}/{self._ab_path}"

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            auth=(self._username, self._password),
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="carddav",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        msg_id = uuid.uuid4().hex[:16]

        vcard = _build_vcard(
            msg_id,
            chunk.manifest_id,
            chunk.block_index,
            chunk.chunk_index,
            encoded_chunk,
        )

        await client.put(
            f"{self._ab_url}{msg_id}.vcf",
            content=vcard,
            headers={"Content-Type": "text/vcard; charset=utf-8"},
        )
        return StoredChunkRef(adapter_name="carddav", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(f"{self._ab_url}{ref.location}.vcf")
        resp.raise_for_status()
        note = _extract_note(resp.text)
        if note is None:
            raise ValueError(f"no NOTE field in vCard for {ref.location}")
        return note

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.request(
                "DELETE", f"{self._ab_url}{ref.location}.vcf"
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        report_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<C:addressbook-query xmlns:D="DAV:" '
            'xmlns:C="urn:ietf:params:xml:ns:carddav">'
            "<D:prop><C:address-data/></D:prop>"
            "<C:filter>"
            '<C:prop-filter name="FN">'
            f'<C:text-match collation="i;unicode-casemap">'
            f"PCHK {manifest_id}"
            "</C:text-match>"
            "</C:prop-filter>"
            "</C:filter>"
            "</C:addressbook-query>"
        )

        resp = await client.request(
            "REPORT",
            self._ab_url,
            content=report_body,
            headers={
                "Content-Type": "application/xml; charset=utf-8",
                "Depth": "1",
            },
        )

        refs: list[StoredChunkRef] = []
        for uid_match in _UID_RE.finditer(resp.text):
            refs.append(
                StoredChunkRef(
                    adapter_name="carddav",
                    location=uid_match.group(1),
                )
            )
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.request(
                "PROPFIND",
                self._ab_url,
                headers={"Depth": "0"},
            )
            return resp.status_code == 207
        except Exception:
            return False
