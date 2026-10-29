"""Qdrant collection-metadata adapter -- stores chunks in Qdrant collection descriptions.

Each write_chunk creates a Qdrant collection whose 'on_disk_payload' field
is False and stores the chunk in the collection description via a point
payload under a separate metadata key, distinct from the main qdrant_adapter.
This adapter uses collection-level metadata (Qdrant aliases as substrate).

Actually uses Qdrant aliases: each chunk becomes an alias name that embeds
the chunk. Aliases are named per-chunk and the original collection is
'picard_meta'. list_chunks scans all aliases for the manifest.

Requires: pip install qdrant-client
"""
from __future__ import annotations

import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "qdrant_alias"
_BASE_COLLECTION = "picard_meta_base"
_DIM = 4


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class QdrantCollectionAdapter(Adapter):

    def __init__(self, *, host: str = "127.0.0.1", port: int = 6333) -> None:
        self._host = host
        self._port = port
        self._client = None

    async def _get_client(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient
            from qdrant_client.models import Distance, VectorParams
            self._client = AsyncQdrantClient(host=self._host, port=self._port)
            # Ensure base collection exists
            try:
                await self._client.get_collection(_BASE_COLLECTION)
            except Exception:
                await self._client.create_collection(
                    collection_name=_BASE_COLLECTION,
                    vectors_config=VectorParams(size=_DIM, distance=Distance.COSINE),
                )
        return self._client

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=256,
            persistent=True,
            ordered=False,
            tags={"requires": "qdrant-client", "substrate": "qdrant-alias"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = await self._get_client()
        # Encode chunk into alias name using base64url to avoid special chars
        import base64
        token = uuid.uuid4().hex[:8]
        chunk_b64 = base64.urlsafe_b64encode(encoded_chunk.encode()).decode().rstrip("=")
        alias_name = f"picard_{token}_{chunk_b64}"
        await client.update_collection_aliases(
            change_aliases_operations=[
                {"create_alias": {
                    "collection_name": _BASE_COLLECTION,
                    "alias_name": alias_name,
                }}
            ]
        )
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"alias": alias_name}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        import base64
        alias_name = json.loads(ref.location)["alias"]
        # Decode from alias name: picard_{8hex}_{base64url_chunk}
        parts = alias_name.split("_", 2)
        if len(parts) < 3:
            raise KeyError(f"qdrant_alias bad alias format: {alias_name}")
        chunk_b64 = parts[2]
        # Re-add padding
        pad = 4 - len(chunk_b64) % 4
        if pad < 4:
            chunk_b64 += "=" * pad
        return base64.urlsafe_b64decode(chunk_b64).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = await self._get_client()
        alias_name = json.loads(ref.location)["alias"]
        await client.update_collection_aliases(
            change_aliases_operations=[
                {"delete_alias": {"alias_name": alias_name}}
            ]
        )
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        import base64
        client = await self._get_client()
        result = await client.get_aliases()
        refs = []
        for alias_info in result.aliases:
            alias_name = alias_info.alias_name
            if not alias_name.startswith("picard_"):
                continue
            try:
                parts = alias_name.split("_", 2)
                if len(parts) < 3:
                    continue
                chunk_b64 = parts[2]
                pad = 4 - len(chunk_b64) % 4
                if pad < 4:
                    chunk_b64 += "=" * pad
                chunk = base64.urlsafe_b64decode(chunk_b64).decode()
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    refs.append(StoredChunkRef(adapter_name=_NAME,
                                               location=json.dumps({"alias": alias_name})))
            except Exception:
                continue
        return refs

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            await client.get_collections()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
