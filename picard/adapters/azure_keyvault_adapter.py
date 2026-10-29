"""Azure Key Vault adapter -- stores chunks as Key Vault secret values.

Each write_chunk creates a secret named picard-{token} in an Azure Key Vault.
Cold reads use the Azure Key Vault REST API.

Requires: aiohttp, vault URI, Azure Bearer token with Key Vault secrets scope.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "azure-keyvault"
_API_VER = "7.4"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class AzureKeyvaultAdapter(Adapter):

    def __init__(self, *, vault_uri: str = "", access_token: str = "") -> None:
        self._vault = vault_uri.rstrip("/")
        self._token = access_token
        self._session = None
        self._reg: dict[str, str] = {}

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
            max_chunk_bytes=25_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "azure-keyvault-token,vault-uri",
                  "substrate": "azure-keyvault-secret"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        name = f"picard-{token}"
        session = await self._get_session()
        async with session.put(
                f"{self._vault}/secrets/{name}",
                params={"api-version": _API_VER},
                headers=self._headers(),
                json={"value": encoded_chunk,
                      "tags": {"manifest": manifest_id}}) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "name": name,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{self._vault}/secrets/{loc['name']}",
                params={"api-version": _API_VER},
                headers=self._headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["value"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.delete(
                f"{self._vault}/secrets/{loc['name']}",
                params={"api-version": _API_VER},
                headers=self._headers()) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t,
                                                     "name": f"picard-{t}",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._vault or not self._token:
            return False
        try:
            session = await self._get_session()
            async with session.get(
                    f"{self._vault}/secrets",
                    params={"api-version": _API_VER, "maxresults": "1"},
                    headers=self._headers()) as resp:
                return resp.status in (200, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
