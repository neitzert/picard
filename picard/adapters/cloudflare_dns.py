"""Cloudflare DNS adapter -- stores chunks as TXT records via the Cloudflare API.

Uses the same FQDN naming scheme as DnsAdapter but replaces RFC 2136
dynamic updates with Cloudflare's REST API.  Each chunk becomes a TXT
record under the configured zone:

    c{chunk_index}.b{block_index}.{manifest_id}.{zone}

StoredChunkRef.location format:
    cf:{record_id}@{fqdn}

Requires: pip install picard[cloudflare]
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


def _pack_location(record_id: str, fqdn: str) -> str:
    return f"cf:{record_id}@{fqdn}"


def _unpack_location(location: str) -> tuple[str, str]:
    """Return (record_id, fqdn) from a packed location string."""
    rest = location.removeprefix("cf:")
    record_id, fqdn = rest.split("@", 1)
    return record_id, fqdn


class CloudflareDnsAdapter(Adapter):
    """Scatter chunks into Cloudflare-managed DNS TXT records."""

    def __init__(
        self,
        zone_id: str,
        api_token: str,
        zone_name: str,
        *,
        max_chunk_bytes: int = 200,
        base_url: str = "https://api.cloudflare.com/client/v4",
    ) -> None:
        self._zone_id = zone_id
        self._api_token = api_token
        self._zone_name = zone_name.rstrip(".")
        self._max = max_chunk_bytes
        self._base_url = base_url.rstrip("/")
        self._client = None

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cloudflare-dns",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1200,
        )

    def _fqdn(self, manifest_id: str, block_index: int, chunk_index: int) -> str:
        return f"c{chunk_index}.b{block_index}.{manifest_id}.{self._zone_name}"

    def _records_url(self, record_id: str | None = None) -> str:
        base = f"{self._base_url}/zones/{self._zone_id}/dns_records"
        if record_id:
            return f"{base}/{record_id}"
        return base

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        fqdn = self._fqdn(chunk.manifest_id, chunk.block_index, chunk.chunk_index)

        resp = await client.post(
            self._records_url(),
            json={
                "type": "TXT",
                "name": fqdn,
                "content": encoded_chunk,
                "ttl": 1,
            },
        )
        resp.raise_for_status()
        record_id = resp.json()["result"]["id"]
        return StoredChunkRef(
            adapter_name="cloudflare-dns",
            location=_pack_location(record_id, fqdn),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = await self._ensure_client()
        record_id, _ = _unpack_location(ref.location)

        resp = await client.get(self._records_url(record_id))
        resp.raise_for_status()
        return resp.json()["result"]["content"]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._ensure_client()
        record_id, _ = _unpack_location(ref.location)

        try:
            resp = await client.delete(self._records_url(record_id))
            return resp.status_code == 200
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = await self._ensure_client()
        suffix = f".{manifest_id}.{self._zone_name}"
        refs: list[StoredChunkRef] = []
        page = 1

        while True:
            resp = await client.get(
                self._records_url(),
                params={
                    "type": "TXT",
                    "per_page": 100,
                    "page": page,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            for record in data["result"]:
                name = record["name"]
                if name.endswith(suffix):
                    refs.append(
                        StoredChunkRef(
                            adapter_name="cloudflare-dns",
                            location=_pack_location(record["id"], name),
                        )
                    )
            info = data.get("result_info", {})
            total_pages = info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            resp = await client.get(
                f"{self._base_url}/zones/{self._zone_id}",
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
