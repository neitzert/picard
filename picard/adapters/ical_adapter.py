"""iCalendar adapter — stores chunks as calendar events.

Chunks are embedded in the DESCRIPTION field of VEVENT components
inside an .ics file.

Supports file persistence when file_path is provided.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ICalAdapter(Adapter):
    """Scatter chunks into iCalendar VEVENT entries."""

    def __init__(
        self,
        calendar_name: str = "PICARD",
        *,
        file_path: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._calendar_name = calendar_name
        self._max = max_chunk_bytes
        self._file_path = Path(file_path) if file_path else None
        self._cache: dict[str, str] = {}
        if self._file_path and self._file_path.exists():
            self._load_from_file()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ical",
            max_chunk_bytes=self._max,
            persistent=self._file_path is not None,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _build_ics(self) -> str:
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        events = ""
        for loc, data in self._cache.items():
            uid = loc.rsplit("/", 1)[-1]
            events += (
                "BEGIN:VEVENT\n"
                f"UID:{uid}@picard\n"
                f"DTSTAMP:{now}\n"
                f"DTSTART:{now}\n"
                f"SUMMARY:PICARD:{uid}\n"
                f"DESCRIPTION:{data}\n"
                "END:VEVENT\n"
            )
        return (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            f"PRODID:-//PICARD//{self._calendar_name}//EN\n"
            f"{events}"
            "END:VCALENDAR\n"
        )

    def _flush(self) -> None:
        if self._file_path is None:
            return
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.write_text(self._build_ics(), encoding="utf-8")

    def _load_from_file(self) -> None:
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            pattern = re.compile(
                r"BEGIN:VEVENT\n.*?UID:(\S+?)@picard\n.*?DESCRIPTION:(.*?)\nEND:VEVENT",
                re.DOTALL,
            )
            for match in pattern.finditer(raw):
                uid = match.group(1)
                data = match.group(2)
                loc = f"ical://{self._calendar_name}/{uid}"
                self._cache[loc] = data
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"ical://{self._calendar_name}/{chunk_id}"
        self._cache[location] = encoded_chunk
        self._flush()
        return StoredChunkRef(adapter_name="ical", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        if self._file_path and self._file_path.exists():
            self._load_from_file()
            if ref.location in self._cache:
                return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        existed = self._cache.pop(ref.location, None) is not None
        if existed:
            self._flush()
        return existed

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ical", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        if self._file_path:
            return self._file_path.parent.exists()
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
