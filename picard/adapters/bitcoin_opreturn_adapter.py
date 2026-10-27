"""Bitcoin OP_RETURN adapter -- embeds chunks in Bitcoin transactions.

Each write broadcasts a transaction with an OP_RETURN output carrying up to
80 bytes of chunk data.  Once mined, the data is permanent and public.

WARNING: Data written on mainnet is PERMANENT and PUBLIC.  It cannot be
deleted -- OP_RETURN outputs are unspendable and stored by every full node
forever.  Use regtest or testnet for development and testing.

Delete is best-effort via RBF (replace-by-fee) while the transaction is
still in the mempool.  After confirmation, delete() returns True but the
data remains in the chain.

Requires: pip install python-bitcoinlib
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef


class BitcoinOpReturnAdapter(Adapter):
    """Scatter chunks into Bitcoin OP_RETURN outputs."""

    def __init__(
        self,
        *,
        rpc_user: str = "bitcoin",
        rpc_password: str = "bitcoin",
        rpc_host: str = "127.0.0.1",
        rpc_port: int = 8332,
        network: str = "regtest",
        max_chunk_bytes: int = 80,
    ) -> None:
        self._rpc_user = rpc_user
        self._rpc_password = rpc_password
        self._rpc_host = rpc_host
        self._rpc_port = rpc_port
        self._network = network
        self._max = max_chunk_bytes
        self._registry: dict[str, str] = {}  # location -> encoded_chunk

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="bitcoin_opreturn",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5,
            tags={
                "requires": "python-bitcoinlib,bitcoin-node",
                "substrate": "bitcoin-opreturn",
                "WARNING": "data-is-permanent-on-mainnet",
            },
        )

    def _proxy(self):
        try:
            import bitcoin.rpc
        except ImportError as e:
            raise ImportError("python-bitcoinlib is required: pip install python-bitcoinlib") from e

        service_url = (
            f"http://{self._rpc_user}:{self._rpc_password}"
            f"@{self._rpc_host}:{self._rpc_port}"
        )
        return bitcoin.rpc.RawProxy(service_url=service_url)

    def _select_network(self):
        try:
            import bitcoin
            if self._network == "testnet":
                bitcoin.SelectParams("testnet")
            elif self._network == "regtest":
                bitcoin.SelectParams("regtest")
            else:
                bitcoin.SelectParams("mainnet")
        except Exception:
            pass

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        data_bytes = encoded_chunk.encode("utf-8")[: self._max]

        def _broadcast():
            self._select_network()
            proxy = self._proxy()

            utxos = proxy.listunspent(1)
            if not utxos:
                raise RuntimeError("no spendable UTXOs available")
            utxo = utxos[0]

            import bitcoin.core
            import bitcoin.core.script

            op_return_out = bitcoin.core.CTxOut(
                0,
                bitcoin.core.script.CScript(
                    [bitcoin.core.script.OP_RETURN, data_bytes]
                ),
            )

            fee_sat = 1000
            change_sat = int(utxo["amount"] * 1e8) - fee_sat
            if change_sat <= 0:
                raise RuntimeError("insufficient funds for fee")

            change_addr = proxy.getrawchangeaddress()
            change_out = bitcoin.core.CTxOut(
                change_sat,
                bitcoin.core.script.CScript.from_address(
                    bitcoin.wallet.CBitcoinAddress(change_addr)
                ) if hasattr(bitcoin.core.script.CScript, "from_address")
                else proxy.getnewaddress(),
            )

            tx_in = bitcoin.core.CMutableTxIn(
                bitcoin.core.COutPoint(
                    bitcoin.core.lx(utxo["txid"]),
                    utxo["vout"],
                )
            )

            tx = bitcoin.core.CMutableTransaction(
                [tx_in], [op_return_out, change_out]
            )

            signed = proxy.signrawtransactionwithwallet(
                bitcoin.core.b2x(tx.serialize())
            )
            txid = proxy.sendrawtransaction(signed["hex"])
            return txid

        txid = await asyncio.to_thread(_broadcast)
        location = f"bitcoin://{txid}"
        self._registry[location] = encoded_chunk
        return StoredChunkRef(adapter_name="bitcoin_opreturn", location=location)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._registry:
            return self._registry[ref.location]

        txid = ref.location.removeprefix("bitcoin://")

        def _fetch():
            self._select_network()
            proxy = self._proxy()
            raw = proxy.getrawtransaction(txid, True)
            for vout in raw.get("vout", []):
                asm = vout.get("scriptPubKey", {}).get("asm", "")
                if asm.startswith("OP_RETURN "):
                    hex_data = asm.split(" ", 1)[1]
                    return bytes.fromhex(hex_data).decode("utf-8", errors="replace")
            raise KeyError(f"no OP_RETURN found in tx {txid}")

        return await asyncio.to_thread(_fetch)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._registry:
            return False

        txid = ref.location.removeprefix("bitcoin://")

        def _rbf():
            try:
                self._select_network()
                proxy = self._proxy()
                raw = proxy.getrawtransaction(txid, True)

                import bitcoin.core
                import bitcoin.core.script

                inputs = [
                    bitcoin.core.CMutableTxIn(
                        bitcoin.core.COutPoint(
                            bitcoin.core.lx(i["txid"]), i["vout"]
                        )
                    )
                    for i in raw.get("vin", [])
                ]
                change_addr = proxy.getrawchangeaddress()
                fee_sat = 2000
                existing_change = sum(
                    int(o["value"] * 1e8)
                    for o in raw.get("vout", [])
                    if "OP_RETURN" not in o.get("scriptPubKey", {}).get("asm", "")
                )
                new_change = existing_change - fee_sat
                if new_change <= 0:
                    return False

                change_out = bitcoin.core.CTxOut(
                    new_change,
                    proxy.getnewaddress(),
                )
                rbf_tx = bitcoin.core.CMutableTransaction(inputs, [change_out])
                for inp in rbf_tx.vin:
                    inp.nSequence = 0xFFFFFFFD

                signed = proxy.signrawtransactionwithwallet(
                    bitcoin.core.b2x(rbf_tx.serialize())
                )
                proxy.sendrawtransaction(signed["hex"])
                return True
            except Exception:
                return True  # best-effort; data may still be mined

        await asyncio.to_thread(_rbf)
        del self._registry[ref.location]
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        from picard.chunker import Chunk

        refs: list[StoredChunkRef] = []
        for loc, data in self._registry.items():
            try:
                if Chunk.decode(data).manifest_id == manifest_id:
                    refs.append(StoredChunkRef(adapter_name="bitcoin_opreturn", location=loc))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        def _check():
            try:
                proxy = self._proxy()
                proxy.getblockchaininfo()
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_check)

    async def close(self) -> None:
        pass
