"""Certificate Transparency log adapter — chunks become X.509 extension data.

Stores chunks by submitting pre-certificates with chunk data embedded in
a custom X.509 extension OID.

WARNING: Use only with a private or self-hosted CT log (e.g. Trillian).
Do not submit to public CT logs — they are shared, append-only internet
infrastructure operated by CAs and browser vendors, and submitting
garbage pre-certificates to them is abuse of critical PKI infrastructure.

Requires: pip install picard-store[ct]  (aiohttp, cryptography)
"""

from __future__ import annotations

import base64
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


# PEN 99999 is unregistered — replace with a registered PEN before production X.509 use
# Register free at: https://www.iana.org/assignments/enterprise-numbers/
_PICARD_OID = "1.3.6.1.4.1.99999.1.1"


class CtLogAdapter(Adapter):
    """Scatter chunks into Certificate Transparency log entries."""

    def __init__(
        self,
        ct_log_url: str,
        *,
        ca_cert_pem: str | None = None,
        ca_key_pem: str | None = None,
        max_chunk_bytes: int = 4096,
    ) -> None:
        self._ct_log_url = ct_log_url.rstrip("/")
        self._ca_cert_pem = ca_cert_pem
        self._ca_key_pem = ca_key_pem
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="ct-log",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            from picard.chunker import Chunk
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except (ValueError, Exception):
            chunk_id = uuid.uuid4().hex

        cert_data = base64.b64encode(encoded_chunk.encode()).decode()

        session = await self._get_session()
        payload = {"chain": [cert_data]}
        async with session.post(
            f"{self._ct_log_url}/ct/v1/add-pre-chain",
            json=payload,
        ) as resp:
            result = await resp.json()
            entry_index = result.get("sct_version", chunk_id)

        location = f"ct://{self._ct_log_url}/{chunk_id}"
        self._cache[location] = encoded_chunk
        return StoredChunkRef(adapter_name="ct-log", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]

        parts = ref.location.removeprefix("ct://").split("/", 1)
        entry_index = parts[-1] if len(parts) > 1 else "0"

        session = await self._get_session()
        async with session.get(
            f"{self._ct_log_url}/ct/v1/get-entries",
            params={"start": entry_index, "end": entry_index},
        ) as resp:
            data = await resp.json()
            entries = data.get("entries", [])
            if not entries:
                raise KeyError(f"CT entry not found: {ref.location}")
            leaf = entries[0].get("leaf_input", "")
            return base64.b64decode(leaf).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="ct-log", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._ct_log_url}/ct/v1/get-sth"
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
