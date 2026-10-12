"""S3 adapter — stores chunks as objects in an AWS S3 bucket.

Each chunk becomes an S3 object keyed by manifest/block/chunk path.
Uses boto3 wrapped in asyncio.to_thread() for async compatibility.

Requires: pip install picard[s3]
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class S3Adapter(Adapter):
    """Scatter chunks into S3 objects."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "picard/",
        region: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        max_chunk_bytes: int = 5_000_000,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._region = region
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._max = max_chunk_bytes
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        import boto3

        kwargs: dict = {"region_name": self._region}
        if self._access_key and self._secret_key:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_key
        self._client = boto3.client("s3", **kwargs)
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}.pchk"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="s3",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=3000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        s3 = self._ensure_client()
        chunk = Chunk.decode(encoded_chunk)
        key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)

        def _put():
            s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=encoded_chunk.encode(),
                ContentType="text/plain",
            )

        await asyncio.to_thread(_put)
        return StoredChunkRef(adapter_name="s3", location=key)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        s3 = self._ensure_client()

        def _get():
            resp = s3.get_object(Bucket=self._bucket, Key=ref.location)
            return resp["Body"].read().decode()

        return await asyncio.to_thread(_get)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        s3 = self._ensure_client()

        def _delete():
            try:
                s3.delete_object(Bucket=self._bucket, Key=ref.location)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        s3 = self._ensure_client()
        prefix = f"{self._prefix}{manifest_id}/"

        def _list():
            refs: list[StoredChunkRef] = []
            kwargs: dict = {
                "Bucket": self._bucket,
                "Prefix": prefix,
            }
            while True:
                resp = s3.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    refs.append(
                        StoredChunkRef(adapter_name="s3", location=obj["Key"])
                    )
                if not resp.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = resp["NextContinuationToken"]
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        s3 = self._ensure_client()

        def _head():
            s3.head_bucket(Bucket=self._bucket)

        try:
            await asyncio.to_thread(_head)
            return True
        except Exception:
            return False
