"""AWS Secrets Manager adapter -- stores chunks as secret values.

Each write_chunk creates a secret named picard/{manifest}/{token}
in AWS Secrets Manager via the REST API with SigV4 signing.

Requires: aiohttp, AWS credentials (access key + secret), region.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef

_NAME = "aws-secretsmanager"
_SERVICE = "secretsmanager"


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
        f"content-type:application/x-amz-json-1.1\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-type;host;x-amz-date;x-amz-target"
    canonical_request = (
        f"{method}\n/\n\n{canonical_headers}\n"
        f"{signed_headers}\n{payload_hash}"
    )
    cred_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    sts = (f"AWS4-HMAC-SHA256\n{amz_date}\n{cred_scope}\n"
           + hashlib.sha256(canonical_request.encode()).hexdigest())

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    sk = _sign(
        _sign(_sign(_sign(f"AWS4{secret_key}".encode(), date_stamp),
                    region), service), "aws4_request")
    sig = hmac.new(sk, sts.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{cred_scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")
    return {"Authorization": auth, "x-amz-date": amz_date,
            "x-amz-target": target,
            "Content-Type": "application/x-amz-json-1.1"}


class AwsSecretsmanagerAdapter(Adapter):

    def __init__(self, *, access_key: str = "", secret_key: str = "",
                 region: str = "us-east-1") -> None:
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._host = f"secretsmanager.{region}.amazonaws.com"
        self._session = None
        self._reg: dict[str, str] = {}

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _call(self, target: str, body: dict) -> tuple[str, dict, bytes]:
        payload = json.dumps(body).encode()
        headers = _sigv4_headers(
            "POST", self._host, self._region, _SERVICE,
            self._access_key, self._secret_key, payload, target,
        )
        return f"https://{self._host}/", headers, payload

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name=_NAME,
            max_chunk_bytes=65536,
            persistent=True,
            ordered=False,
            rate_limit_per_min=100,
            tags={"requires": "aws-credentials",
                  "substrate": "aws-secrets-manager"},
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        token = uuid.uuid4().hex[:16]
        manifest_id = _manifest_id_from_chunk(encoded_chunk)
        name = f"picard/{manifest_id}/{token}"
        url, headers, payload = self._call(
            "secretsmanager.CreateSecret",
            {"Name": name, "SecretString": encoded_chunk,
             "Tags": [{"Key": "picard_manifest", "Value": manifest_id}]},
        )
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            resp.raise_for_status()
        self._reg[token] = encoded_chunk
        return StoredChunkRef(adapter_name=_NAME,
                              location=json.dumps({"token": token, "name": name,
                                                    "manifest": manifest_id}))

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        loc = json.loads(ref.location)
        if loc["token"] in self._reg:
            return self._reg[loc["token"]]
        url, headers, payload = self._call(
            "secretsmanager.GetSecretValue", {"SecretId": loc["name"]})
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        chunk = data["SecretString"]
        self._reg[loc["token"]] = chunk
        return chunk

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        loc = json.loads(ref.location)
        self._reg.pop(loc["token"], None)
        url, headers, payload = self._call(
            "secretsmanager.DeleteSecret",
            {"SecretId": loc["name"], "ForceDeleteWithoutRecovery": True})
        session = await self._get_session()
        async with session.post(url, headers=headers, data=payload) as resp:
            return resp.status in (200, 404)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [StoredChunkRef(adapter_name=_NAME,
                               location=json.dumps({"token": t,
                                                     "name": f"picard/{manifest_id}/{t}",
                                                     "manifest": manifest_id}))
                for t, c in self._reg.items() if _manifest_id_from_chunk(c) == manifest_id]

    async def health_check(self) -> bool:
        if not self._access_key or not self._secret_key:
            return False
        try:
            url, headers, payload = self._call(
                "secretsmanager.ListSecrets", {"MaxResults": 1})
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
