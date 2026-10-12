"""CalDAV adapter — stores chunks as calendar event descriptions.

Each chunk becomes a VEVENT on a CalDAV calendar, with the encoded
chunk data in the DESCRIPTION field and metadata in the SUMMARY.

Requires: pip install picard-store[caldav]  (httpx)
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_DESCRIPTION_RE = re.compile(r"DESCRIPTION:(.*?)(?:\r?\n(?! )|\Z)", re.DOTALL)


def _build_ical(msg_id: str, manifest_id: str, block: int, chunk: int, data: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//PICARD//CHUNK//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{msg_id}@picard\r\n"
        "DTSTART:20000101T000000Z\r\n"
        "DTEND:20000101T000100Z\r\n"
        f"SUMMARY:PCHK {manifest_id} b{block} c{chunk}\r\n"
        f"DESCRIPTION:{data}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _parse_description(ical_text: str) -> str:
    m = _DESCRIPTION_RE.search(ical_text)
    if not m:
        raise ValueError("no DESCRIPTION found in iCalendar data")
    value = m.group(1)
    return value.replace("\r\n ", "").replace("\n ", "")


class CalDavAdapter(Adapter):
    """Scatter chunks into CalDAV calendar events."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        calendar_path: str = "calendars/picard/",
        max_chunk_bytes: int = 50_000,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._calendar_path = calendar_path.strip("/")
        self._max = max_chunk_bytes
        self._client: object | None = None

    def _cal_url(self) -> str:
        return f"{self._url}/{self._calendar_path}"

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            auth=(self._username, self._password),
            timeout=30.0,
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="caldav",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=60,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        msg_id = uuid.uuid4().hex[:16]

        ical = _build_ical(
            msg_id, chunk.manifest_id,
            chunk.block_index, chunk.chunk_index,
            encoded_chunk,
        )

        await client.put(
            f"{self._cal_url()}/{msg_id}.ics",
            content=ical,
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        return StoredChunkRef(adapter_name="caldav", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        resp = await client.get(f"{self._cal_url()}/{ref.location}.ics")
        resp.raise_for_status()
        return _parse_description(resp.text)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        try:
            resp = await client.delete(f"{self._cal_url()}/{ref.location}.ics")
            return resp.status_code in (200, 204)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        propfind_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:">'
            "<d:prop><d:displayname/></d:prop>"
            "</d:propfind>"
        )
        resp = await client.request(
            "PROPFIND",
            f"{self._cal_url()}/",
            content=propfind_body,
            headers={"Content-Type": "application/xml", "Depth": "1"},
        )

        refs: list[StoredChunkRef] = []
        try:
            root = ET.fromstring(resp.text)
            ns = {"d": "DAV:"}
            for response_el in root.findall(".//d:response", ns):
                href_el = response_el.find("d:href", ns)
                if href_el is None or href_el.text is None:
                    continue
                href = href_el.text
                if not href.endswith(".ics"):
                    continue
                uid = href.rsplit("/", 1)[-1].replace(".ics", "")
                event_resp = await client.get(f"{self._cal_url()}/{uid}.ics")
                if event_resp.status_code == 200:
                    text = event_resp.text
                    if f"PCHK {manifest_id}" in text:
                        refs.append(
                            StoredChunkRef(adapter_name="caldav", location=uid)
                        )
        except ET.ParseError:
            pass

        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.request(
                "PROPFIND",
                f"{self._cal_url()}/",
                content='<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
                headers={"Content-Type": "application/xml", "Depth": "0"},
            )
            return resp.status_code == 207
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
