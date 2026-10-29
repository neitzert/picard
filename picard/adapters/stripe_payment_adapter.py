"""Stripe PaymentIntent adapter -- stores chunks in PaymentIntent metadata.

Each write_chunk creates a PaymentIntent with chunk data in metadata.
Cold reads fetch via GET /v1/payment_intents/{id}.

Requires: aiohttp, a Stripe secret key.
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "stripe-payment"
_BASE_URL = "https://api.stripe.com/v1"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class StripePaymentAdapter(Adapter):

    def __init__(self, *, secret_key: str = "") -> None:
        self._secret_key = secret_key
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            auth = aiohttp.BasicAuth(self._secret_key, "")
            self._session = aiohttp.ClientSession(auth=auth)
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=40960,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aiohttp,stripe-secret-key", "substrate": "stripe-payment-intent-metadata",
                  "warning": "creates-real-payment-intents"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        session = await self._get_session()
        data = {
            "amount": "1",
            "currency": "usd",
            "metadata[token]": token,
            "metadata[chunk]": encoded_chunk,
            "metadata[manifest]": manifest_id,
        }
        async with session.post(f"{_BASE_URL}/payment_intents",
                                data=data) as resp:
            resp.raise_for_status()
            body = await resp.json()
        pi_id = body["id"]
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "pi_id": pi_id,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        session = await self._get_session()
        async with session.get(
                f"{_BASE_URL}/payment_intents/{loc['pi_id']}") as resp:
            resp.raise_for_status()
            body = await resp.json()
        chunk = body["metadata"]["chunk"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        session = await self._get_session()
        async with session.post(
                f"{_BASE_URL}/payment_intents/{loc['pi_id']}/cancel") as resp:
            resp.raise_for_status()
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        if not self._secret_key:
            return []
        import urllib.parse
        session = await self._get_session()
        query = urllib.parse.quote(f"metadata['manifest']:'{manifest_id}'")
        refs = []
        url = f"{_BASE_URL}/payment_intents/search?query={query}&limit=100"
        while url:
            async with session.get(url) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
            for pi in data.get("data", []):
                token = (pi.get("metadata") or {}).get("token", "")
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "pi_id": pi["id"],
                                          "manifest": manifest_id}),
                ))
            url = data.get("next_page")
        return refs

    async def health_check(self) -> bool:
        if not self._secret_key:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{_BASE_URL}/balance") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
