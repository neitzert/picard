"""Storj object adapter -- stores chunks as objects on Storj decentralized cloud.

Uses the Storj uplink CLI or S3-compatible gateway. Defaults to the S3 gateway
approach (endpoint configurable) so it works out of the box with aiohttp.

Requires: aiohttp, valid Storj S3 gateway credentials (access key + secret).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "storj-object"
_DEFAULT_ENDPOINT = "https://gateway.storjshare.io"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _aws_sign_headers(
    method: str,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    payload: bytes,
    endpoint: str,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    host = endpoint.replace("https://", "").replace("http://", "")
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_uri = f"/{bucket}/{key}"
    canonical_request = (
        f"{method}\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    signing_key = _sign(
        _sign(
            _sign(
                _sign(f"AWS4{secret_key}".encode(), date_stamp),
                region,
            ),
            "s3",
        ),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Host": host,
    }


class StorjObjectAdapter(Adapter):

    def __init__(
        self,
        *,
        access_key: str = "",
        secret_key: str = "",
        bucket: str = "picard",
        region: str = "us-east-1",
        endpoint: str = _DEFAULT_ENDPOINT,
    ) -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region = region
        self._endpoint = endpoint.rstrip("/")
        self._reg: dict[str, str] = {}
        self._session = None

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _url(self, key: str) -> str:
        return f"{self._endpoint}/{self._bucket}/{key}"

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=10_000_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=300,
            tags={"requires": "aiohttp,storj-credentials",
                  "substrate": "storj-s3-object"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        key = f"picard/{manifest_id}/{token}"
        payload = encoded_chunk.encode("utf-8")
        headers = _aws_sign_headers(
            "PUT", self._bucket, key, self._region,
            self._access_key, self._secret_key, payload, self._endpoint,
        )
        headers["Content-Type"] = "application/octet-stream"
        session = await self._get_session()
        async with session.put(self._url(key),
                                data=payload, headers=headers) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(
            adapter_name=_NAME,
            location=json.dumps({"token": token, "key": key,
                                  "manifest": manifest_id}),
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        token = loc["token"]
        if token in self._reg:
            return self._reg[token]
        headers = _aws_sign_headers(
            "GET", self._bucket, loc["key"], self._region,
            self._access_key, self._secret_key, b"", self._endpoint,
        )
        session = await self._get_session()
        async with session.get(self._url(loc["key"]),
                                headers=headers) as resp:
            resp.raise_for_status()
            chunk = (await resp.read()).decode("utf-8")
        self._reg[token] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        headers = _aws_sign_headers(
            "DELETE", self._bucket, loc["key"], self._region,
            self._access_key, self._secret_key, b"", self._endpoint,
        )
        session = await self._get_session()
        async with session.delete(self._url(loc["key"]),
                                   headers=headers) as resp:
            return resp.status in (200, 204, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for token, chunk in self._reg.items():
            if _manifest_id_from_chunk(chunk) == manifest_id:
                refs.append(StoredChunkRef(
                    adapter_name=_NAME,
                    location=json.dumps({
                        "token": token,
                        "key": f"picard/{manifest_id}/{token}",
                        "manifest": manifest_id,
                    }),
                ))
        return refs

    async def health_check(self) -> bool:
        if not self._access_key or not self._secret_key:
            return False
        try:
            headers = _aws_sign_headers(
                "HEAD", self._bucket, "", self._region,
                self._access_key, self._secret_key, b"", self._endpoint,
            )
            session = await self._get_session()
            async with session.head(f"{self._endpoint}/{self._bucket}",
                                     headers=headers) as resp:
                return resp.status in (200, 301, 403)
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
