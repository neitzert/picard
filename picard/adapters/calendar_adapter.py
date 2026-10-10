"""Google Calendar adapter — stores chunks as calendar event descriptions.

Creates calendar events with chunk data in the description field.

Requires: pip install picard[gcal]  (google-api-python-client, google-auth)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class GoogleCalendarAdapter(Adapter):
    """Scatter chunks into Google Calendar event descriptions."""

    def __init__(
        self,
        credentials_json: str,
        *,
        calendar_id: str = "primary",
        max_chunk_bytes: int = 8192,
    ) -> None:
        self._credentials_json = credentials_json
        self._calendar_id = calendar_id
        self._max = max_chunk_bytes
        self._service = None
        self._cache: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="google-calendar",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    def _get_service(self):
        if self._service is None:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_service_account_file(
                self._credentials_json,
                scopes=["https://www.googleapis.com/auth/calendar"],
            )
            self._service = build("calendar", "v3", credentials=creds)
        return self._service

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        def _create():
            service = self._get_service()
            event = {
                "summary": f"picard-{chunk_id}",
                "description": encoded_chunk,
                "start": {"dateTime": "2099-01-01T00:00:00Z"},
                "end": {"dateTime": "2099-01-01T01:00:00Z"},
                "visibility": "private",
            }
            result = (
                service.events()
                .insert(calendarId=self._calendar_id, body=event)
                .execute()
            )
            return result["id"]

        event_id = await asyncio.to_thread(_create)
        location = f"gcal://{self._calendar_id}/{event_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="google-calendar", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        parts = ref.location.removeprefix("gcal://").split("/", 1)
        cal_id = parts[0]
        event_id = parts[1] if len(parts) > 1 else ""

        def _read():
            service = self._get_service()
            event = (
                service.events()
                .get(calendarId=cal_id, eventId=event_id)
                .execute()
            )
            return event.get("description", "")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        self._cache.pop(ref.location, None)

        parts = ref.location.removeprefix("gcal://").split("/", 1)
        cal_id = parts[0]
        event_id = parts[1] if len(parts) > 1 else ""

        def _delete():
            service = self._get_service()
            service.events().delete(
                calendarId=cal_id, eventId=event_id
            ).execute()

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        def _list():
            service = self._get_service()
            results = (
                service.events()
                .list(
                    calendarId=self._calendar_id,
                    q=f"picard-{manifest_id}",
                    maxResults=2500,
                )
                .execute()
            )
            refs = []
            for event in results.get("items", []):
                refs.append(
                    StoredChunkRef(
                        adapter_name="google-calendar",
                        location=f"gcal://{self._calendar_id}/{event['id']}",
                    )
                )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            service = self._get_service()
            service.calendarList().list(maxResults=1).execute()
            return True

        try:
            return await asyncio.to_thread(_check)
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
