"""NEAR Protocol adapter — stores chunks in function call args or contract state.

Each chunk is sent as arguments to a FunctionCall action on a storage
contract.  Reads use a view call to query state.  Once included in a
block the data is permanent.

Requires: pip install picard[near]   (aiohttp)
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class NearAdapter(Adapter):
    """Scatter chunks into NEAR Protocol contract state."""

    def __init__(
        self,
        rpc_url: str = "https://rpc.mainnet.near.org",
        account_id: str = "",
        private_key: str = "",
        *,
        contract_id: str = "",
        max_chunk_bytes: int = 4_194_304,
    ) -> None:
        self._rpc_url = rpc_url
        self._account_id = account_id
        self._private_key = private_key
        self._contract_id = contract_id or account_id
        self._max = max_chunk_bytes
        self._session = None

    async def _ensure_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def _rpc(self, method: str, params: dict | list) -> dict:
        session = await self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with session.post(self._rpc_url, json=payload) as resp:
            return await resp.json()

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="near",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=30,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        chunk_key = uuid.uuid4().hex
        args = base64.b64encode(
            json.dumps({"key": chunk_key, "data": encoded_chunk}).encode()
        ).decode()
        await self._rpc(
            "broadcast_tx_commit",
            [
                {
                    "signer_id": self._account_id,
                    "receiver_id": self._contract_id,
                    "actions": [
                        {
                            "type": "FunctionCall",
                            "method_name": "store_chunk",
                            "args": args,
                            "gas": "30000000000000",
                            "deposit": "0",
                        }
                    ],
                }
            ],
        )
        return StoredChunkRef(
            adapter_name="near", location=f"near://{chunk_key}"
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        chunk_key = ref.location.removeprefix("near://")
        args = base64.b64encode(
            json.dumps({"key": chunk_key}).encode()
        ).decode()
        result = await self._rpc(
            "query",
            {
                "request_type": "call_function",
                "finality": "final",
                "account_id": self._contract_id,
                "method_name": "get_chunk",
                "args_base64": args,
            },
        )
        res = result.get("result", {})
        raw = res.get("result", [])
        if not raw:
            raise ValueError(f"no chunk data for key {chunk_key}")
        decoded = bytes(raw).decode()
        payload = json.loads(decoded)
        return payload.get("data", decoded)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        chunk_key = ref.location.removeprefix("near://")
        args = base64.b64encode(
            json.dumps({"key": chunk_key}).encode()
        ).decode()
        try:
            await self._rpc(
                "broadcast_tx_commit",
                [
                    {
                        "signer_id": self._account_id,
                        "receiver_id": self._contract_id,
                        "actions": [
                            {
                                "type": "FunctionCall",
                                "method_name": "delete_chunk",
                                "args": args,
                                "gas": "30000000000000",
                                "deposit": "0",
                            }
                        ],
                    }
                ],
            )
            return True
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return []

    async def health_check(self) -> bool:
        try:
            result = await self._rpc("status", [])
            return "result" in result
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": None,
        }
