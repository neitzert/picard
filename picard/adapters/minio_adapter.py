"""MinIO adapter — stores chunks in a self-hosted S3-compatible store.

MinIO is common in on-prem and edge deployments. This adapter uses the
minio Python SDK for native S3-compatible access.

Requires: pip install picard[minio]   (minio)
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class MinioAdapter(Adapter):
    """Scatter chunks into a MinIO instance."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        bucket: str = "picard",
        secure: bool = True,
        prefix: str = "chunks/",
        max_chunk_bytes: int = 5_368_709_120,
    ) -> None:
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        from minio import Minio

        self._client = Minio(
            self._endpoint,
            access_key=self._access_key,
            secret_key=self._secret_key,
            secure=self._secure,
        )
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}.pchk"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="minio",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=5000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        client = self._ensure_client()
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            key = f"{self._prefix}{uuid.uuid4().hex}.pchk"

        body = encoded_chunk.encode()

        def _put():
            client.put_object(
                self._bucket,
                key,
                io.BytesIO(body),
                length=len(body),
                content_type="text/plain",
            )

        await asyncio.to_thread(_put)
        return StoredChunkRef(adapter_name="minio", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        client = self._ensure_client()

        def _get():
            response = client.get_object(self._bucket, ref.location)
            try:
                return response.read().decode()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        client = self._ensure_client()

        def _delete():
            try:
                client.remove_object(self._bucket, ref.location)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        client = self._ensure_client()
        prefix = f"{self._prefix}{manifest_id}/"

        def _list():
            refs: list[StoredChunkRef] = []
            for obj in client.list_objects(self._bucket, prefix=prefix):
                refs.append(
                    StoredChunkRef(adapter_name="minio", location=obj.object_name)
                )
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        client = self._ensure_client()

        try:
            await asyncio.to_thread(
                lambda: client.bucket_exists(self._bucket)
            )
            return True
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        client = self._ensure_client()

        def _capacity():
            count = 0
            total = 0
            for obj in client.list_objects(self._bucket, prefix=self._prefix, recursive=True):
                count += 1
                total += obj.size or 0
            return count, total

        try:
            count, total = await asyncio.to_thread(_capacity)
            return {
                "used_bytes": total,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": count,
            }
        except Exception:
            return {
                "used_bytes": None,
                "free_bytes": None,
                "total_bytes": None,
                "chunk_count": None,
            }
