"""Google Sheets adapter -- stores chunks as spreadsheet cell values.

Each write_chunk writes the encoded chunk to the next available row in
column A of a configured sheet via the Google Sheets API v4.

Requires: aiohttp, Google OAuth2 Bearer token with spreadsheets scope,
spreadsheet_id.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "google-sheets"
_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class GoogleSheetsAdapter(Adapter):

    def __init__(self, *, access_token: str = "",
                 spreadsheet_id: str = "") -> None:
        self._token = access_token
        self._spreadsheet_id = spreadsheet_id
        self._session = None
        self._reg: dict[str, tuple[str, int]] = {}
        self._row_counter = 1

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=50000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
            tags={"requires": "google-sheets-token,spreadsheet-id",
                  "substrate": "google-sheets-cell"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        self._row_counter += 1
        row = self._row_counter
        cell_range = f"Sheet1!A{row}:C{row}"
        session = await self._get_session()
        async with session.put(
                f"{_BASE}/{self._spreadsheet_id}/values/{cell_range}",
                params={"valueInputOption": "RAW"},
                headers=self._headers(),
                json={"values": [[token, manifest_id, encoded_chunk]]}) as resp:
            resp.raise_for_status()
        self._reg[token] = (encoded_chunk, row)
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "row": row,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token][0]
        session = await self._get_session()
        row = loc["row"]
        cell_range = f"Sheet1!C{row}"
        async with session.get(
                f"{_BASE}/{self._spreadsheet_id}/values/{cell_range}",
                headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        values = data.get("values", [])
        if not values or not values[0]:
            raise KeyError(f"google-sheets chunk not found: {token}")
        chunk = values[0][0]
        self._reg[token] = (chunk, row)
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        row = loc["row"]
        cell_range = f"Sheet1!A{row}:C{row}"
        async with session.put(
                f"{_BASE}/{self._spreadsheet_id}/values/{cell_range}",
                params={"valueInputOption": "RAW"},
                headers=self._headers(),
                json={"values": [["", "", ""]]}) as resp:
            return resp.status == 200

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t, "row": r,
                                                     "manifest": manifest_id}))
                for t, (c, r) in self._reg.items()
                if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._token or not self._spreadsheet_id:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{_BASE}/{self._spreadsheet_id}",
                    headers=self._headers()) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
