"""AWS SNS adapter — stores chunks as SNS message attributes.

Publishes chunks as messages to an SNS topic. The chunk data
rides in the message body and can be fanned out to SQS queues,
Lambda functions, HTTP endpoints — the data disperses automatically.

Requires: pip install picard[aws]  (aiohttp)
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import quote

import aiohttp

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class AwsSnsAdapter(Adapter):
    """Scatter chunks via AWS SNS topic messages."""

    def __init__(
        self,
        topic_arn: str,
        access_key_id: str,
        secret_access_key: str,
        *,
        region: str = "us-east-1",
        max_chunk_bytes: int = 262144,
    ) -> None:
        self._topic_arn = topic_arn
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region
        self._max = max_chunk_bytes
        self._cache: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="aws-sns",
            max_chunk_bytes=self._max,
            persistent=False,
            ordered=False,
            rate_limit_per_min=300,
            tags={"cloud": "aws"},
        )

    def _sign_v4(self, method: str, url: str, body: str, now: datetime) -> dict[str, str]:
        datestamp = now.strftime("%Y%m%d")
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        host = f"sns.{self._region}.amazonaws.com"
        service = "sns"
        credential_scope = f"{datestamp}/{self._region}/{service}/aws4_request"

        canonical_headers = f"host:{host}\nx-amz-date:{amzdate}\n"
        signed_headers = "host;x-amz-date"
        payload_hash = hashlib.sha256(body.encode()).hexdigest()

        canonical_request = "\n".join([
            method, "/", "", canonical_headers, signed_headers, payload_hash,
        ])

        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amzdate, credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _sign(f"AWS4{self._secret_access_key}".encode(), datestamp)
        k_region = _sign(k_date, self._region)
        k_service = _sign(k_region, service)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "x-amz-date": amzdate,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            key = f"{chunk.manifest_id}/{chunk.block_index}/{chunk.chunk_index}"
        except ValueError:
            key = uuid.uuid4().hex

        body = (
            f"Action=Publish"
            f"&TopicArn={quote(self._topic_arn, safe='')}"
            f"&Subject={quote(f'picard:{key}', safe='')}"
            f"&Message={quote(encoded_chunk, safe='')}"
        )
        now = datetime.now(timezone.utc)
        endpoint = f"https://sns.{self._region}.amazonaws.com/"
        headers = self._sign_v4("POST", endpoint, body, now)

        session = await self._get_session()
        resp = await session.post(endpoint, headers=headers, data=body)
        resp.raise_for_status()
        text = await resp.text()

        import re
        m = re.search(r"<MessageId>(.*?)</MessageId>", text)
        msg_id = m.group(1) if m else key

        loc = f"sns://{self._topic_arn}/{msg_id}"
        self._cache[loc] = encoded_chunk
        return StoredChunkRef(adapter_name="aws-sns", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        if ref.location in self._cache:
            return self._cache[ref.location]
        raise KeyError(f"SNS is fire-and-forget; chunk not in cache: {ref.location}")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        return self._cache.pop(ref.location, None) is not None

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        return [
            StoredChunkRef(adapter_name="aws-sns", location=loc)
            for loc in self._cache
            if manifest_id in loc
        ]

    async def health_check(self) -> bool:
        try:
            body = "Action=ListTopics"
            now = datetime.now(timezone.utc)
            endpoint = f"https://sns.{self._region}.amazonaws.com/"
            headers = self._sign_v4("POST", endpoint, body, now)
            session = await self._get_session()
            resp = await session.post(endpoint, headers=headers, data=body)
            return resp.status == 200
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        return {
            "used_bytes": sum(len(v.encode()) for v in self._cache.values()),
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": len(self._cache),
        }
