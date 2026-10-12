"""Cloudflare R2 adapter — S3-compatible object store with zero egress fees.

Chunks live in R2 buckets.  R2 speaks S3, so the same adapter code
works with any S3-compatible store.  Zero egress fees make retrieval cheap.

Requires: pip install picard[r2]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import quote

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class CloudflareR2Adapter(Adapter):
    """Scatter chunks into Cloudflare R2 buckets."""

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        *,
        bucket: str = "picard",
        prefix: str = "chunks/",
        max_chunk_bytes: int = 5_368_709_120,
    ) -> None:
        self._account_id = account_id
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket = bucket
        self._prefix = prefix
        self._max = max_chunk_bytes
        self._endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self._session = None


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="cloudflare-r2",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
        )

    def _object_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{self._prefix}{chunk.manifest_id}/b{chunk.block_index}_c{chunk.chunk_index}.pchk"
        except ValueError:
            return f"{self._prefix}{uuid.uuid4().hex}.pchk"

    def _sign_v4(self, method: str, path: str, headers: dict, payload: bytes) -> dict:
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        region = "auto"
        service = "s3"

        headers["x-amz-date"] = amz_date
        headers["x-amz-content-sha256"] = hashlib.sha256(payload).hexdigest()

        signed_headers = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(
            f"{k}:{v}\n" for k, v in sorted(headers.items())
        )
        canonical_request = (
            f"{method}\n{quote(path, safe='/')}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{headers['x-amz-content-sha256']}"
        )
        scope = f"{datestamp}/{region}/{service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            + hashlib.sha256(canonical_request.encode()).hexdigest()
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k = _sign(f"AWS4{self._secret_access_key}".encode(), datestamp)
        k = _sign(k, region)
        k = _sign(k, service)
        k = _sign(k, "aws4_request")
        signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._object_key(encoded_chunk)
        url = f"{self._endpoint}/{self._bucket}/{key}"
        payload = encoded_chunk.encode()
        headers = {"host": f"{self._account_id}.r2.cloudflarestorage.com"}
        headers = self._sign_v4("PUT", f"/{self._bucket}/{key}", headers, payload)

        session = await self._get_session()
        async with session.put(url, data=payload, headers=headers) as resp:
            resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="cloudflare-r2",
            location=f"r2://{self._bucket}/{key}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.removeprefix(f"r2://{self._bucket}/")
        url = f"{self._endpoint}/{self._bucket}/{key}"
        headers = {"host": f"{self._account_id}.r2.cloudflarestorage.com"}
        headers = self._sign_v4("GET", f"/{self._bucket}/{key}", headers, b"")

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return (await resp.read()).decode()

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.removeprefix(f"r2://{self._bucket}/")
        url = f"{self._endpoint}/{self._bucket}/{key}"
        headers = {"host": f"{self._account_id}.r2.cloudflarestorage.com"}
        headers = self._sign_v4("DELETE", f"/{self._bucket}/{key}", headers, b"")

        try:
            session = await self._get_session()
            async with session.delete(url, headers=headers) as resp:
                return resp.status < 400
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{self._prefix}{manifest_id}/"
        url = f"{self._endpoint}/{self._bucket}?list-type=2&prefix={quote(prefix, safe='')}"
        headers = {"host": f"{self._account_id}.r2.cloudflarestorage.com"}
        headers = self._sign_v4("GET", f"/{self._bucket}", headers, b"")

        session = await self._get_session()
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            text = await resp.text()

        import re
        keys = re.findall(r"<Key>(.*?)</Key>", text)
        return [
            StoredChunkRef(
                adapter_name="cloudflare-r2",
                location=f"r2://{self._bucket}/{k}",
            )
            for k in keys
        ]

    async def health_check(self) -> bool:
        url = f"{self._endpoint}/{self._bucket}"
        headers = {"host": f"{self._account_id}.r2.cloudflarestorage.com"}
        headers = self._sign_v4("HEAD", f"/{self._bucket}", headers, b"")

        try:
            session = await self._get_session()
            async with session.head(url, headers=headers) as resp:
                return resp.status == 200
        except Exception:
            return False
