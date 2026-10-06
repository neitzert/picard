"""NFC/NDEF adapter — stores chunks as NDEF records on NFC tags.

Each chunk becomes an NDEF record with an external type "picard.chunk".
A cooperating NFC reader/writer handles the physical tag interaction.

The internal _write_tag / _read_tag / _format_tag methods are abstract
and intended to be mocked or overridden with a real NFC library.
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class NfcNdefAdapter(Adapter):
    """Scatter chunks into NDEF records on NFC tags."""

    def __init__(
        self,
        *,
        reader_name: str | None = None,
        max_chunk_bytes: int = 800,
    ) -> None:
        self._reader_name = reader_name
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="nfc-ndef",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def _write_tag(self, records: list[dict]) -> bool:
        raise NotImplementedError("requires NFC library")

    async def _read_tag(self) -> list[dict]:
        raise NotImplementedError("requires NFC library")

    async def _format_tag(self) -> bool:
        raise NotImplementedError("requires NFC library")

    async def _detect_reader(self) -> bool:
        raise NotImplementedError("requires NFC library")

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        msg_id = uuid.uuid4().hex[:16]
        record = {
            "tnf": 0x04,
            "type": "picard.chunk",
            "id": msg_id,
            "payload": encoded_chunk,
        }
        await self._write_tag([record])
        return StoredChunkRef(adapter_name="nfc-ndef", location=msg_id)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        records = await self._read_tag()
        for rec in records:
            if rec.get("id") == ref.location:
                return rec["payload"]
        raise ValueError(f"NDEF record not found: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        try:
            await self._format_tag()
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        try:
            records = await self._read_tag()
        except Exception:
            return []

        prefix = f"PCHK:1:{manifest_id}:"
        refs: list[StoredChunkRef] = []
        for rec in records:
            if rec.get("type") != "picard.chunk":
                continue
            payload = rec.get("payload", "")
            if payload.startswith(prefix):
                refs.append(
                    StoredChunkRef(
                        adapter_name="nfc-ndef",
                        location=rec["id"],
                    )
                )
        return refs

    async def health_check(self) -> bool:
        try:
            await self._detect_reader()
            return True
        except Exception:
            return False
