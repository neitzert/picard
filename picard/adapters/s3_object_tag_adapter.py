"""AWS S3 adapter -- stores chunks as S3 object bodies with manifest tagging."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "s3-object-tag"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


class S3ObjectTagAdapter(Adapter):

    def __init__(self, *, bucket: str = "",
                 aws_access_key_id: str = "",
                 aws_secret_access_key: str = "",
                 region_name: str = "us-east-1") -> None:
        self._bucket = bucket
        self._key_id = aws_access_key_id
        self._secret = aws_secret_access_key
        self._region = region_name
        self._reg: dict[str, str] = {}

    def _client(self):
        import boto3
        return boto3.client(
            "s3",
            aws_access_key_id=self._key_id or None,
            aws_secret_access_key=self._secret or None,
            region_name=self._region,
        )

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=5_000_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=3500,
            tags={"requires": "boto3", "substrate": "s3-object"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        s3_key = f"picard/{manifest_id}/{token}"
        self._reg[token] = encoded_chunk
        if self._bucket:
            def _put():
                s3 = self._client()
                s3.put_object(
                    Bucket=self._bucket,
                    Key=s3_key,
                    Body=encoded_chunk.encode("utf-8"),
                    Tagging=f"picard_manifest={manifest_id}&picard_token={token}",
                )
            await asyncio.to_thread(_put)
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": s3_key,
                                 "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        if self._bucket:
            def _get():
                s3 = self._client()
                obj = s3.get_object(Bucket=self._bucket, Key=loc["key"])
                return obj["Body"].read().decode("utf-8")
            chunk = await asyncio.to_thread(_get)
            self._reg[token] = chunk
            return chunk
        raise KeyError(f"s3-object-tag chunk not found: {token}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        if self._bucket:
            def _del():
                s3 = self._client()
                s3.delete_object(Bucket=self._bucket, Key=loc["key"])
            await asyncio.to_thread(_del)
        return True

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        if self._bucket:
            def _list():
                s3 = self._client()
                paginator = s3.get_paginator("list_objects_v2")
                results = []
                for page in paginator.paginate(
                        Bucket=self._bucket,
                        Prefix=f"picard/{manifest_id}/"):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        token = key.split("/")[-1]
                        results.append((token, key))
                return results
            pairs = await asyncio.to_thread(_list)
            for token, key in pairs:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({"token": token, "key": key,
                                         "manifest": manifest_id}),
                ))
        else:
            for token, chunk in self._reg.items():
                if _manifest_id_from_chunk(chunk) == manifest_id:
                    refs.append(StoredChunkRef(
                        adapter_name=_NAME,
                        location=json.dumps(
                            {"token": token,
                             "key": f"picard/{manifest_id}/{token}",
                             "manifest": manifest_id}),
                    ))
        return refs

    async def health_check(self) -> bool:
        if not self._bucket:
            return True
        try:
            def _hc():
                s3 = self._client()
                s3.head_bucket(Bucket=self._bucket)
            await asyncio.to_thread(_hc)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        pass
