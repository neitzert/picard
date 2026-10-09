"""AWS Lambda adapter — stores chunks by invoking Lambda functions.

Chunks are written and read via Lambda invocations.  The Lambda function
acts as a serverless KV store — data lives in the container's /tmp or
in the function's environment.  Ephemeral but very hard to detect.

Requires: pip install picard[lambda]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class LambdaAdapter(Adapter):
    """Scatter chunks via AWS Lambda invocations."""

    def __init__(
        self,
        function_name: str,
        *,
        region: str = "us-east-1",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        max_chunk_bytes: int = 6_291_456,
    ) -> None:
        self._function_name = function_name
        self._region = region
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._max = max_chunk_bytes
        self._endpoint = (
            f"https://lambda.{region}.amazonaws.com"
            f"/2015-03-31/functions/{function_name}/invocations"
        )
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
            name="aws-lambda",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=500,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"picard/{chunk.manifest_id}/b{chunk.block_index}_c{chunk.chunk_index}"
        except ValueError:
            return f"picard/{uuid.uuid4().hex}"

    def _sign_request(self, payload: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        datestamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        service = "lambda"

        host = f"lambda.{self._region}.amazonaws.com"
        content_hash = hashlib.sha256(payload).hexdigest()

        headers = {
            "host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": content_hash,
            "content-type": "application/json",
        }

        signed_headers = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
        path = f"/2015-03-31/functions/{self._function_name}/invocations"
        canonical_request = (
            f"POST\n{path}\n\n{canonical_headers}\n{signed_headers}\n{content_hash}"
        )
        scope = f"{datestamp}/{self._region}/{service}/aws4_request"
        string_to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            + hashlib.sha256(canonical_request.encode()).hexdigest()
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k = _sign(f"AWS4{self._secret_key}".encode(), datestamp)
        k = _sign(k, self._region)
        k = _sign(k, service)
        k = _sign(k, "aws4_request")
        signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)
        payload = json.dumps(
            {"action": "write", "key": key, "data": encoded_chunk}
        ).encode()
        headers = self._sign_request(payload)

        session = await self._get_session()
        async with session.post(
            self._endpoint, data=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
        return StoredChunkRef(
            adapter_name="aws-lambda",
            location=f"lambda://{self._function_name}/{key}",
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        key = ref.location.split("/", 2)[-1] if "://" in ref.location else ref.location
        payload = json.dumps({"action": "read", "key": key}).encode()
        headers = self._sign_request(payload)

        session = await self._get_session()
        async with session.post(
            self._endpoint, data=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
            data = result.get("data")
            if data is None:
                raise KeyError(f"chunk not found: {key}")
            return data

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        key = ref.location.split("/", 2)[-1] if "://" in ref.location else ref.location
        payload = json.dumps({"action": "delete", "key": key}).encode()
        headers = self._sign_request(payload)

        try:
            session = await self._get_session()
            async with session.post(
                self._endpoint, data=payload, headers=headers
            ) as resp:
                result = await resp.json()
                return result.get("deleted", False)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        payload = json.dumps(
            {"action": "list", "prefix": f"picard/{manifest_id}/"}
        ).encode()
        headers = self._sign_request(payload)

        session = await self._get_session()
        async with session.post(
            self._endpoint, data=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
        return [
            StoredChunkRef(
                adapter_name="aws-lambda",
                location=f"lambda://{self._function_name}/{k}",
            )
            for k in result.get("keys", [])
        ]

    async def health_check(self) -> bool:
        payload = json.dumps({"action": "ping"}).encode()
        headers = self._sign_request(payload)

        try:
            session = await self._get_session()
            async with session.post(
                self._endpoint, data=payload, headers=headers
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
