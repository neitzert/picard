"""Wasabi adapter — stores chunks in Wasabi (S3-compatible, no egress fees).

Uses S3v4 signature for authentication against Wasabi's S3-compatible API.

Requires: pip install picard[wasabi]   (boto3)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class WasabiAdapter(Adapter):
    """Scatter chunks into Wasabi cloud storage."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        region: str = "us-east-1",
        endpoint_url: str = "https://s3.wasabisys.com",
        prefix: str = "picard/",
        max_chunk_bytes: int = 5_368_709_120,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._client: object | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        import boto3

        self._client = boto3.client(
            "s3",
            region_name=self._region,
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )
        return self._client

    def _key(self, manifest_id: str, block: int, chunk_index: int) -> str:
        return f"{self._prefix}{manifest_id}/b{block}_c{chunk_index}.pchk"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="wasabi",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        s3 = self._ensure_client()
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = self._key(chunk.manifest_id, chunk.block_index, chunk.chunk_index)
        except ValueError:
            key = f"{self._prefix}{uuid.uuid4().hex}.pchk"

        def _put():
            s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=encoded_chunk.encode(),
                ContentType="text/plain",
            )

        await asyncio.to_thread(_put)
        return StoredChunkRef(adapter_name="wasabi", location=key)

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
            kwargs: dict = {"Bucket": self._bucket, "Prefix": prefix}
            while True:
                resp = s3.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    refs.append(
                        StoredChunkRef(adapter_name="wasabi", location=obj["Key"])
                    )
                if not resp.get("IsTruncated"):
                    break
                kwargs["ContinuationToken"] = resp["NextContinuationToken"]
            return refs

        return await asyncio.to_thread(_list)

    async def health_check(self) -> bool:
        s3 = self._ensure_client()

        try:
            await asyncio.to_thread(
                lambda: s3.head_bucket(Bucket=self._bucket)
            )
            return True
        except Exception:
            return False
