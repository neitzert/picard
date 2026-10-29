"""AWS DynamoDB adapter -- stores chunks as DynamoDB table items.

Each write_chunk puts an item {pk: token, chunk: encoded_chunk, manifest: id}
into a DynamoDB table via the AWS REST API with SigV4 signing.

Requires: aiohttp, AWS credentials (access key + secret), region, table name.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "aws-dynamodb"
_SERVICE = "dynamodb"


def _manifest_id_from_chunk(encoded_chunk: str) -> str:
    try:
        parts = encoded_chunk.split(":")
        if parts[0] == "PCHK" and len(parts) >= 3:
            return parts[2]
    except Exception:
        pass
    return ""


def _sigv4_headers(
    method: str,
    host: str,
    path: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    payload: bytes,
    target: str,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_headers = (
        f"content-type:application/x-amz-json-1.0\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-type;host;x-amz-date;x-amz-target"
    canonical_request = (
        f"{method}\n{path}\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    sig_key = _sign(
        _sign(_sign(_sign(f"AWS4{secret_key}".encode(), date_stamp),
                    region), service), "aws4_request")
    signature = hmac.new(sig_key, string_to_sign.encode(),
                          hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    return {
        "Authorization": auth,
        "x-amz-date": amz_date,
        "x-amz-target": target,
        "Content-Type": "application/x-amz-json-1.0",
    }


class AwsDynamodbAdapter(Adapter):

    def __init__(self, *, access_key: str = "", secret_key: str = "",
                 region: str = "us-east-1",
                 table_name: str = "picard_chunks") -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._table = table_name
        self._host = f"dynamodb.{region}.amazonaws.com"
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _call(self, target: str, body: dict) -> tuple[str, dict, bytes]:
        payload = json.dumps(body).encode()
        url = f"https://{self._host}/"
        headers = _sigv4_headers(
            "POST", self._host, "/", self._region, _SERVICE,
            self._access_key, self._secret_key, payload, target,
        )
        return url, headers, payload

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=400_000,
            persistent=True,
            ordered=False,
            rate_limit_per_min=1000,
            tags={"requires": "aws-credentials",
                  "substrate": "aws-dynamodb-item"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        url, headers, payload = self._call(
            "DynamoDB_20120810.PutItem",
            {"TableName": self._table,
             "Item": {
                 "pk": {"S": token},
                 "chunk": {"S": encoded_chunk},
                 "manifest": {"S": manifest_id},
             }},
        )
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        url, headers, payload = self._call(
            "DynamoDB_20120810.GetItem",
            {"TableName": self._table,
             "Key": {"pk": {"S": loc["token"]}}},
        )
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        item = data.get("Item", {})
        chunk = item.get("chunk", {}).get("S", "")
        if not chunk:
            raise KeyError(f"aws-dynamodb chunk not found: {loc['token']}")
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        url, headers, payload = self._call(
            "DynamoDB_20120810.DeleteItem",
            {"TableName": self._table,
             "Key": {"pk": {"S": loc["token"]}}},
        )
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t,
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._access_key or not self._secret_key:
            return False
        try:
            url, headers, payload = self._call(
                "DynamoDB_20120810.ListTables", {"Limit": 1})
            session = await self._get_session()
            async with session.post(url, headers=headers, data=payload) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def close(self) -> None:
        self._reg.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
