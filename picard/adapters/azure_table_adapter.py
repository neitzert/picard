"""Azure Table Storage adapter -- stores chunks as Azure Table entities.

Each write_chunk inserts an entity with PartitionKey=manifest_id,
RowKey=token, and Chunk=encoded_chunk. Cold reads query by RowKey.

Requires: aiohttp, an Azure Storage account name and shared-key or SAS token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from email.utils import formatdate
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "azure-table"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AzureTableAdapter(Adapter):

    def __init__(self, *, account_name: str = "", sas_token: str = "",
                 table_name: str = "picardchunks") -> None:
        self._account_name = account_name
        self._sas_token = sas_token.lstrip("?")
        self._table_name = table_name
        self._reg: dict[str, str] = {}
        self._session = None

    def _base_url(self) -> str:
        return f"https://{self._account_name}.table.core.windows.net"

    def _table_url(self) -> str:
        return f"{self._base_url()}/{self._table_name}?{self._sas_token}"

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            headers = {
                "Accept": "application/json;odata=nometadata",
                "Content-Type": "application/json",
                "x-ms-version": "2020-12-06",
            }
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=63488,
            persistent=True,
            ordered=False,
            rate_limit_per_min=2000,
            tags={"requires": "aiohttp,azure-storage-sas", "substrate": "azure-table-entity"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        entity = {
            "PartitionKey": manifest_id or "default",
            "RowKey": token,
            "Chunk": encoded_chunk,
        }
        url = f"{self._base_url()}/{self._table_name}?{self._sas_token}"
        async with session.post(url, json=entity) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token,
                                  "partition": manifest_id or "default",
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        url = (f"{self._base_url()}/{self._table_name}"
               f"(PartitionKey='{loc['partition']}',RowKey='{loc['token']}')"
               f"?{self._sas_token}")
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["Chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        url = (f"{self._base_url()}/{self._table_name}"
               f"(PartitionKey='{loc['partition']}',RowKey='{loc['token']}')"
               f"?{self._sas_token}")
        async with session.delete(
                url, headers={"If-Match": "*"}) as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        session = await self._get_session()
        filt = f"PartitionKey eq '{manifest_id or 'default'}'"
        import urllib.parse
        url = (f"{self._base_url()}/{self._table_name}()"
               f"?{self._sas_token}&$filter={urllib.parse.quote(filt)}")
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return [
            StoredChunkRef(
                adapter_name=_NAME,
                location=json.dumps({"token": row["RowKey"],
                                      "partition": row["PartitionKey"],
                                      "manifest": manifest_id}),
            )
            for row in data.get("value", [])
        ]

    async def health_check(self) -> bool:
        if not self._account_name or not self._sas_token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._base_url()}/{self._table_name}?{self._sas_token}&$top=1") as resp:
                return resp.status in (200, 404)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
