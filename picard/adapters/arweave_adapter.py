"""Arweave adapter -- stores chunks on the permaweb.

Pay once, store forever.  Each chunk becomes an Arweave transaction
with the data field containing the encoded chunk.

Requires: pip install picard[arweave]  (aiohttp, cryptography)
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class ArweaveAdapter(Adapter):
    """Scatter chunks onto the Arweave permaweb."""

    def __init__(
        self,
        wallet_jwk: dict,
        *,
        gateway_url: str = "https://arweave.net",
        max_chunk_bytes: int = 12_000_000,
    ) -> None:
        self._wallet = wallet_jwk
        self._gateway = gateway_url.rstrip("/")
        self._max = max_chunk_bytes
        self._session: object | None = None

    async def _ensure_session(self):
        if self._session is not None:
            return self._session
        import aiohttp

        self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="arweave",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    def _b64url(self, data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _b64url_decode(self, s: str) -> bytes:
        padding = 4 - len(s) % 4
        return base64.urlsafe_b64decode(s + "=" * padding)

    def _get_address(self) -> str:
        n_bytes = self._b64url_decode(self._wallet["n"])
        return self._b64url(hashlib.sha256(n_bytes).digest())

    async def _sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        n = int.from_bytes(self._b64url_decode(self._wallet["n"]), "big")
        e = int.from_bytes(self._b64url_decode(self._wallet["e"]), "big")
        d = int.from_bytes(self._b64url_decode(self._wallet["d"]), "big")
        p = int.from_bytes(self._b64url_decode(self._wallet["p"]), "big")
        q = int.from_bytes(self._b64url_decode(self._wallet["q"]), "big")

        dp = d % (p - 1)
        dq = d % (q - 1)
        qi = pow(q, -1, p)

        private_key = rsa.RSAPrivateNumbers(p, q, d, dp, dq, qi, rsa.RSAPublicNumbers(e, n)).private_key()

        return private_key.sign(message, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.AUTO,
        ), hashes.SHA256())

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data_bytes = encoded_chunk.encode()
        session = await self._ensure_session()

        async with session.get(f"{self._gateway}/price/{len(data_bytes)}") as resp:
            reward = (await resp.text()).strip()

        async with session.get(f"{self._gateway}/tx_anchor") as resp:
            last_tx = (await resp.text()).strip()

        tx = {
            "format": 2,
            "owner": self._wallet["n"],
            "target": "",
            "quantity": "0",
            "reward": reward,
            "last_tx": last_tx,
            "data": self._b64url(data_bytes),
            "data_size": str(len(data_bytes)),
            "data_root": self._b64url(hashlib.sha256(data_bytes).digest()),
            "tags": [
                {
                    "name": self._b64url(b"Content-Type"),
                    "value": self._b64url(b"application/octet-stream"),
                },
                {
                    "name": self._b64url(b"App-Name"),
                    "value": self._b64url(b"PICARD"),
                },
            ],
        }

        sign_data = (
            b"2"
            + self._b64url_decode(tx["owner"])
            + self._b64url_decode(tx["target"])
            + tx["quantity"].encode()
            + tx["reward"].encode()
            + self._b64url_decode(tx["last_tx"])
            + self._b64url_decode(tx["data_root"])
        )

        signature = await self._sign(sign_data)
        tx["signature"] = self._b64url(signature)
        tx["id"] = self._b64url(hashlib.sha256(signature).digest())

        async with session.post(
            f"{self._gateway}/tx",
            json=tx,
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()

        loc = f"ar://{tx['id']}"
        return StoredChunkRef(adapter_name="arweave", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_id = ref.location.removeprefix("ar://")
        session = await self._ensure_session()

        async with session.get(f"{self._gateway}/{tx_id}") as resp:
            if resp.status == 404:
                raise KeyError(f"chunk not found: {ref.location}")
            resp.raise_for_status()
            data = await resp.read()
            return data.decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        query = """
        query($owners: [String!]) {
            transactions(
                owners: $owners,
                tags: [{name: "App-Name", values: ["PICARD"]}],
                first: 100
            ) {
                edges { node { id } }
            }
        }
        """
        variables = {"owners": [self._get_address()]}

        try:
            session = await self._ensure_session()
            async with session.post(
                f"{self._gateway}/graphql",
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            refs = []
            for edge in data.get("data", {}).get("transactions", {}).get("edges", []):
                tx_id = edge["node"]["id"]
                refs.append(StoredChunkRef(
                    adapter_name="arweave",
                    location=f"ar://{tx_id}",
                ))
            return refs
        except Exception:
            return []

    async def health_check(self) -> bool:
        try:
            session = await self._ensure_session()
            async with session.get(f"{self._gateway}/info") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        try:
            session = await self._ensure_session()
            async with session.get(
                f"{self._gateway}/wallet/{self._get_address()}/balance"
            ) as resp:
                balance = int((await resp.text()).strip())
            return {
                "used_bytes": None,
                "free_bytes": balance,
                "total_bytes": None,
                "chunk_count": None,
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
