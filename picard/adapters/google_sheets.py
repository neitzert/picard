"""Google Sheets adapter — stores chunks as spreadsheet row values.

Each chunk becomes a row: [chunk_key, encoded_data, timestamp].
Reads scan column A for the chunk key and return column B.

Requires: pip install picard[gsheets]  (google-api-python-client google-auth)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class GoogleSheetsAdapter(Adapter):
    """Scatter chunks into Google Sheets rows."""

    def __init__(
        self,
        credentials_json: str,
        spreadsheet_id: str,
        *,
        sheet_name: str = "picard",
        max_chunk_bytes: int = 50000,
    ) -> None:
        self._credentials_json = credentials_json
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name
        self._max = max_chunk_bytes
        self._service: object | None = None

    def _ensure_service(self):
        if self._service is not None:
            return self._service
        import json

        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_service_account_info(
            json.loads(self._credentials_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="google-sheets",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        ts = datetime.now(timezone.utc).isoformat()
        row = [[key, encoded_chunk, ts]]

        def _write():
            svc = self._ensure_service()
            svc.spreadsheets().values().append(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:C",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": row},
            ).execute()

        await asyncio.to_thread(_write)
        loc = f"gsheets://{self._spreadsheet_id}/{self._sheet_name}!{key}"
        return StoredChunkRef(adapter_name="google-sheets", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.rsplit("!", 1)[-1]

        def _read():
            svc = self._ensure_service()
            result = svc.spreadsheets().values().get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:B",
            ).execute()
            for row in result.get("values", []):
                if len(row) >= 2 and row[0] == key:
                    return row[1]
            raise KeyError(f"chunk not found: {key}")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.rsplit("!", 1)[-1]

        def _delete():
            svc = self._ensure_service()
            result = svc.spreadsheets().values().get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:B",
            ).execute()
            for i, row in enumerate(result.get("values", [])):
                if len(row) >= 1 and row[0] == key:
                    svc.spreadsheets().values().update(
                        spreadsheetId=self._spreadsheet_id,
                        range=f"{self._sheet_name}!A{i+1}:C{i+1}",
                        valueInputOption="RAW",
                        body={"values": [["", "", ""]]},
                    ).execute()
                    return True
            return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}:"

        def _list():
            svc = self._ensure_service()
            result = svc.spreadsheets().values().get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:A",
            ).execute()
            refs: list[StoredChunkRef] = []
            for row in result.get("values", []):
                if row and row[0].startswith(prefix):
                    loc = f"gsheets://{self._spreadsheet_id}/{self._sheet_name}!{row[0]}"
                    refs.append(StoredChunkRef(adapter_name="google-sheets", location=loc))
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        def _check():
            svc = self._ensure_service()
            svc.spreadsheets().get(
                spreadsheetId=self._spreadsheet_id,
            ).execute()
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _capacity():
            svc = self._ensure_service()
            result = svc.spreadsheets().values().get(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._sheet_name}!A:A",
            ).execute()
            count = len([r for r in result.get("values", []) if r and r[0]])
            return count

        try:
            count = await asyncio.to_thread(_capacity)
        except Exception:
            count = 0
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": count,
        }
