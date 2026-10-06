"""iCalendar adapter — stores chunks as calendar events.

Chunks are embedded in the DESCRIPTION field of VEVENT components
inside an .ics file.

Pure in-memory — no CalDAV server required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class ICalAdapter(Adapter):
    """Scatter chunks into iCalendar VEVENT entries."""

    def __init__(
        self,
        calendar_name: str = "PICARD",
        *,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._calendar_name = calendar_name
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ical",
            max_chunk_bytes=self._max,
            persistent=False,
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

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_id = uuid.uuid4().hex[:12]
        location = f"ical://{self._calendar_name}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="ical", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"chunk not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ical", location=loc)
            for loc in self._cache
        ]

    async def health_check(self) -> bool:
        return True

    async def capacity(self) -> dict[str, int | None]:
        used = sum(len(v.encode()) for v in self._cache.values())
        return {
            "used_bytes": used,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
