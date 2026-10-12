"""Cardano adapter — stores chunks in transaction metadata (auxiliary data).

Each chunk is placed as a metadata value under label 8888 in a Cardano
transaction.  Uses the Blockfrost API for both submission and retrieval.
Once confirmed, data is permanent on-chain.

Requires: pip install picard[cardano]   (aiohttp, pycardano)
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class CardanoAdapter(Adapter):
    """Scatter chunks into Cardano transaction metadata."""

    METADATA_LABEL = 8888

    def __init__(
        self,
        api_url: str = "https://cardano-mainnet.blockfrost.io/api/v0",
        project_id: str = "",
        payment_key_hex: str = "",
        *,
        max_chunk_bytes: int = 16_384,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._project_id = project_id
        self._payment_key_hex = payment_key_hex
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={"project_id": self._project_id}
            )
        return self._session

    async def _get(self, path: str) -> dict | list:
        session = await self._ensure_session()
        url = f"{self._api_url}{path}"
        async with session.get(url) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cardano",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=10,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        import pycardano as pc

        is_testnet = "testnet" in self._api_url or "preview" in self._api_url
        network = pc.Network.TESTNET if is_testnet else pc.Network.MAINNET

        sk = pc.PaymentSigningKey.from_primitive(
            bytes.fromhex(self._payment_key_hex)
        )
        vk = pc.PaymentVerificationKey.from_signing_key(sk)
        address = pc.Address(payment_part=vk.hash(), network=network)

        context = pc.BlockFrostChainContext(
            project_id=self._project_id,
            base_url=self._api_url + "/",
        )

        metadata = pc.Metadata({self.METADATA_LABEL: {"chunk": encoded_chunk}})
        aux_data = pc.AuxiliaryData(pc.AlonzoMetadata(metadata=metadata))

        builder = pc.TransactionBuilder(context)
        builder.add_input_address(address)
        builder.auxiliary_data = aux_data

        def _build_and_submit():
            tx = builder.build_and_sign([sk], change_address=address)
            context.submit_tx(tx)
            return str(tx.id)

        tx_hash = await asyncio.to_thread(_build_and_submit)
        return StoredChunkRef(adapter_name="cardano", location=f"ada://{tx_hash}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        tx_hash = ref.location.removeprefix("ada://")
        result = await self._get(f"/txs/{tx_hash}/metadata")
        if isinstance(result, list):
            for entry in result:
                if entry.get("label") == str(self.METADATA_LABEL):
                    value = entry.get("json_metadata", {})
                    if isinstance(value, str):
                        return value
                    return value.get("chunk", "")
        raise ValueError(f"no metadata label {self.METADATA_LABEL} in tx {tx_hash}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._get("/health")
            return result.get("is_healthy", False)
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
