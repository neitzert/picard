"""Stellar adapter — stores chunks in manage_data operations.

Each chunk is split into 64-byte segments, each stored as a manage_data
operation in a single transaction.  Up to 100 operations per transaction
allows up to 6 400 bytes per chunk write.

Writes require a funded account.  Reads are free via Horizon.
Once included in a ledger the data is permanent (delete_chunk → False).

Requires: pip install picard[stellar]   (aiohttp, stellar-sdk)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class StellarAdapter(Adapter):
    """Scatter chunks into Stellar manage_data operations."""

    def __init__(
        self,
        horizon_url: str = "https://horizon.stellar.org",
        secret_key: str = "",
        *,
        network: str = "mainnet",
        max_chunk_bytes: int = 6_144,
    ) -> None:
        self._horizon_url = horizon_url.rstrip("/")
        self._secret_key = secret_key
        self._network = network
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(self, path: str) -> dict:
        session = await self._ensure_session()
        url = f"{self._horizon_url}{path}"
        async with session.get(url) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="stellar",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from stellar_sdk import Keypair, Network, Server, TransactionBuilder

        keypair = Keypair.from_secret(self._secret_key)
        data_bytes = encoded_chunk.encode()
        chunk_id = hashlib.sha256(data_bytes).hexdigest()[:16]
        segments = [data_bytes[i : i + 64] for i in range(0, len(data_bytes), 64)]
        network_pass = (
            Network.PUBLIC_NETWORK_PASSPHRASE
            if self._network == "mainnet"
            else Network.TESTNET_NETWORK_PASSPHRASE
        )

        def _build_and_submit():
            server = Server(horizon_url=self._horizon_url)
            account = server.load_account(keypair.public_key)
            builder = TransactionBuilder(
                source_account=account,
                network_passphrase=network_pass,
                base_fee=100,
            )
            for idx, seg in enumerate(segments):
                builder.append_manage_data_op(
                    data_name=f"pcd-{chunk_id}-{idx:04d}",
                    data_value=seg,
                )
            tx = builder.build()
            tx.sign(keypair)
            resp = server.submit_transaction(tx)
            return resp["hash"]

        tx_hash = await asyncio.to_thread(_build_and_submit)
        return StoredChunkRef(adapter_name="stellar", location=f"xlm://{tx_hash}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_hash = ref.location.removeprefix("xlm://")
        result = await self._get(f"/transactions/{tx_hash}/operations?limit=100")
        records = result.get("_embedded", {}).get("records", [])

        segments: dict[int, bytes] = {}
        for rec in records:
            if rec.get("type") == "manage_data":
                name = rec.get("name", "")
                if name.startswith("pcd-") and name.count("-") >= 2:
                    try:
                        idx = int(name.rsplit("-", 1)[1])
                        val_b64 = rec.get("value", "")
                        if val_b64:
                            segments[idx] = base64.b64decode(val_b64)
                    except (ValueError, IndexError):
                        pass

        if not segments:
            raise ValueError(f"no picard data in tx {tx_hash}")
        return b"".join(segments[i] for i in sorted(segments)).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._get("/")
            return "horizon_version" in result
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
