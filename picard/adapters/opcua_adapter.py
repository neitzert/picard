"""OPC UA adapter — stores chunks as Variable node values on industrial servers.

Each chunk becomes a String-typed Variable under a picard folder in the
server's address space.  The node identifier encodes manifest/block/chunk.

Requires: pip install picard-store[opcua]  (asyncua)
"""

from __future__ import annotations

import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class OpcuaAdapter(Adapter):
    """Scatter chunks into OPC UA variable nodes."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        namespace_uri: str = "urn:picard",
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._url = endpoint_url
        self._username = username
        self._password = password
        self._ns_uri = namespace_uri
        self._max = max_chunk_bytes
        self._written: dict[str, str] = {}

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="opcua",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=120,
        )

    def _node_id(self, key: str, ns_idx: int) -> str:
        return f"ns={ns_idx};s=picard/{key}"

    async def _connect(self):
        from asyncua import Client

        client = Client(url=self._url)
        if self._username:
            client.set_user(self._username)
            client.set_password(self._password or "")
        await client.connect()
        return client

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        from asyncua import ua

        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        client = await self._connect()
        try:
            ns_idx = await client.get_namespace_index(self._ns_uri)
            objects = client.nodes.objects

            node_id = self._node_id(key, ns_idx)
            var = client.get_node(node_id)
            try:
                await var.write_value(ua.Variant(encoded_chunk, ua.VariantType.String))
            except Exception:
                folder = await objects.add_folder(ns_idx, "picard")
                var = await folder.add_variable(node_id, key, encoded_chunk)
                await var.set_writable()
                await var.write_value(
                    ua.Variant(encoded_chunk, ua.VariantType.String)
                )
        finally:
            await client.disconnect()

        loc = f"opcua://{self._url}/{node_id}"
        self._written[loc] = node_id
        return StoredChunkRef(adapter_name="opcua", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        node_id = self._written[ref.location]
        client = await self._connect()
        try:
            var = client.get_node(node_id)
            value = await var.read_value()
            return str(value)
        finally:
            await client.disconnect()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        if ref.location not in self._written:
            return False
        node_id = self._written.pop(ref.location)
        client = await self._connect()
        try:
            var = client.get_node(node_id)
            await client.delete_nodes([var])
            return True
        except Exception:
            return False
        finally:
            await client.disconnect()

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="opcua", location=loc)
            for loc in self._written
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            client = await self._connect()
            server_node = client.get_node("i=2256")
            await server_node.read_value()
            await client.disconnect()
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        total = sum(len(v.encode()) for v in self._written.values())
        return {
            "used_bytes": total,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._written),
        }
