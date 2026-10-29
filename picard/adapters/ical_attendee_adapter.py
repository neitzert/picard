"""iCalendar attendee adapter -- stores chunks in X-PICARD-CHUNK property.

Each write_chunk creates a .ics file with a VEVENT that has an X-PICARD-CHUNK
property holding the encoded chunk. The iCalendar format supports arbitrary
X- extension properties.

No external dependencies -- stdlib only (re, uuid, os, tempfile).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "ical_attendee"

_CHUNK_RE = re.compile(r"^X-PICARD-CHUNK:(.+)$", re.MULTILINE)


def _make_ics(token: str, encoded_chunk: str) -> str:
    # iCalendar line length limit is 75 octets; fold long lines with CRLF + SPACE
    def fold(line: str) -> str:
        if len(line.encode("utf-8")) <= 75:
            return line
        out = []
        while len(line.encode("utf-8")) > 75:
            cut = 74
            # Ensure we don't split a multibyte char
            while len(line[:cut].encode("utf-8")) > 74:
                cut -= 1
            out.append(line[:cut])
            line = " " + line[cut:]
        out.append(line)
        return "\r\n".join(out)

    chunk_line = fold(f"X-PICARD-CHUNK:{encoded_chunk}")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//PICARD//PICARD//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{token}@picard\r\n"
        "DTSTART:20240101T000000Z\r\n"
        "DTEND:20240101T010000Z\r\n"
        "SUMMARY:PICARD chunk\r\n"
        f"{chunk_line}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _parse_ics(raw: str) -> str:
    # Unfold continuation lines (CRLF + SPACE or LF + SPACE)
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    m = _CHUNK_RE.search(unfolded)
    if not m:
        raise KeyError("no X-PICARD-CHUNK property in iCalendar file")
    return m.group(1)


class IcalAttendeeAdapter(Adapter):
    """Scatter chunks in iCalendar VEVENT X-PICARD-CHUNK extension properties."""

    def __init__(self, *, store_dir: str | None = None) -> None:
        self._dir = store_dir or tempfile.mkdtemp(prefix="picard_ical_")
        self._reg: dict[str, tuple[str, str]] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=32768,
            persistent=True,
            ordered=False,
            tags={"requires": "stdlib", "substrate": "ical-x-property"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        def _write():
            token = uuid.uuid4().hex
            path = os.path.join(self._dir, f"{token}.ics")
            with open(path, "w", encoding="utf-8") as f:
                f.write(_make_ics(token, encoded_chunk))
            return token, path

        token, path = await asyncio.to_thread(_write)
        self._reg[token] = (path, encoded_chunk)
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "path": path}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][1]

        def _read():
            with open(loc["path"], "r", encoding="utf-8") as f:
                return _parse_ics(f.read())

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        token = loc["token"]
        entry = self._reg.pop(token, None)
        path = entry[0] if entry else loc.get("path")
        if path and os.path.exists(path):
            os.unlink(path)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        refs = []
        for token, (path, data) in self._reg.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(
                        StoredChunkRef(
                            adapter_name=_NAME,
                            location=json.dumps({"token": token, "path": path}),
                        )
                    )
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass
