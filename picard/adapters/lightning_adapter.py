"""Core Lightning hold-invoice adapter -- stores chunks as CLN hold invoices.

Each write creates a hold invoice on the configured CLN node using the
holdinvoice plugin.  The payment hash is derived from the chunk token.
Because CLN invoice descriptions have limited capacity and no structured
binary field, the encoded chunk is held in an in-process registry; the
lightning invoice acts as a durable commitment anchor that survives node
restarts (visible in `lightning-cli listinvoices`).

WARNING: Requires a running Core Lightning node with the holdinvoice plugin
installed.  See https://github.com/ElementsProject/lightning/tree/master/plugins/holdinvoice

Requires: pip install pyln-client
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class LightningAdapter(Adapter):
    """Scatter chunks as CLN hold invoices."""

    def __init__(
        self,
        *,
        socket_path: str = "~/.lightning/bitcoin/lightning-rpc",
        max_chunk_bytes: int = 512,
        amount_msat: int = 1000,
        expiry_seconds: int = 2_592_000,
    ) -> None:
        self._socket_path = os.path.expanduser(socket_path)
        self._max = max_chunk_bytes
        self._amount_msat = amount_msat
        self._expiry = expiry_seconds
        self._registry: dict[str, tuple[str, str]] = {}  # location -> (encoded_chunk, label)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="lightning",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=30,
            tags={
                "requires": "pyln-client,CLN-node,holdinvoice-plugin",
                "substrate": "lightning-hold-invoice",
            },
        )

    def _rpc(self):
        try:
            import pyln.client
        except ImportError as e:
            raise ImportError("pyln-client is required: pip install pyln-client") from e
        return pyln.client.LightningRpc(self._socket_path)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from picard.chunker import Chunk

        token = uuid.uuid4().hex[:16]
        payment_hash = hashlib.sha256(token.encode()).hexdigest()
        location = f"lightning://{payment_hash}"

        try:
            manifest_id = Chunk.decode(encoded_chunk).manifest_id
        except Exception:
            manifest_id = ""

        label = f"PICARD:{token}"
        description = f"PICARD chunk mid={manifest_id} tok={token}"
        # payment_hash = sha256(token), so preimage = token (32 hex chars -> 16 bytes)
        preimage = token.ljust(64, "0")  # pad to 32-byte hex

        def _call():
            rpc = self._rpc()
            try:
                rpc.call("holdinvoice", {
                    "payment_hash": payment_hash,
                    "amount_msat": self._amount_msat,
                    "label": label,
                    "description": description,
                    "expiry": self._expiry,
                })
            except Exception as hold_err:
                if "Unknown command" in str(hold_err) or "Method not found" in str(hold_err):
                    # holdinvoice plugin not loaded; fall back to regular invoice
                    rpc.call("invoice", {
                        "amount_msat": self._amount_msat,
                        "label": label,
                        "description": description,
                        "expiry": self._expiry,
                        "preimage": preimage,
                    })
                else:
                    raise

        await asyncio.to_thread(_call)
        self._registry[location] = (encoded_chunk, label)
        return StoredChunkRef(adapter_name="lightning", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location not in self._registry:
            raise KeyError(f"chunk not in registry: {ref.location}")
        return self._registry[ref.location][0]

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False
        payment_hash = ref.location.removeprefix("lightning://")
        _, label = self._registry[ref.location]

        def _cancel():
            try:
                rpc = self._rpc()
                try:
                    rpc.call("cancelinvoice", {"payment_hash": payment_hash})
                except Exception:
                    rpc.call("delinvoice", {"label": label, "status": "unpaid"})
            except Exception:
                pass

        await asyncio.to_thread(_cancel)
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        refs: list[StoredChunkRef] = []
        for loc, (data, _label) in self._registry.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="lightning", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        def _check():
            try:
                rpc = self._rpc()
                rpc.getinfo()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    async def close(self) -> None:
        pass
