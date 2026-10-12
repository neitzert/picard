"""AWS SQS adapter — stores chunks as SQS messages.

Each chunk becomes a message with the chunk data as body and a
MessageAttribute holding the chunk key for retrieval. Messages use
long visibility timeout to stay available.

Requires: pip install picard[aws]  (aiobotocore)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class AwsSqsAdapter(Adapter):
    """Scatter chunks into AWS SQS messages."""

    def __init__(
        self,
        queue_url: str,
        *,
        region: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        visibility_timeout: int = 43200,
        max_chunk_bytes: int = 262_144,
    ) -> None:
        self._queue_url = queue_url
        self._region = region
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._visibility = visibility_timeout
        self._max = max_chunk_bytes
        self._message_cache: dict[str, str] = {}

    def _get_client(self):
        import boto3

        kwargs: dict = {"region_name": self._region, "service_name": "sqs"}
        if self._access_key and self._secret_key:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_key
        return boto3.client(**kwargs)

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="aws-sqs",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=3000,
        )

    def _chunk_key(self, encoded_chunk: str) -> str:
        try:
            chunk = Chunk.decode(encoded_chunk)
            return f"{chunk.manifest_id}:{chunk.block_index}:{chunk.chunk_index}"
        except ValueError:
            return uuid.uuid4().hex

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        key = self._chunk_key(encoded_chunk)

        def _send():
            client = self._get_client()
            resp = client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=encoded_chunk,
                MessageAttributes={
                    "picard_key": {
                        "DataType": "String",
                        "StringValue": key,
                    }
                },
            )
            return resp["MessageId"]

        msg_id = await asyncio.to_thread(_send)
        loc = f"sqs://{self._queue_url}/{msg_id}"
        self._message_cache[msg_id] = encoded_chunk
        return StoredChunkRef(adapter_name="aws-sqs", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        msg_id = ref.location.rsplit("/", 1)[-1]
        if msg_id in self._message_cache:
            return self._message_cache[msg_id]

        def _receive():
            client = self._get_client()
            resp = client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=10,
                MessageAttributeNames=["picard_key"],
                VisibilityTimeout=self._visibility,
            )
            for msg in resp.get("Messages", []):
                if msg["MessageId"] == msg_id:
                    return msg["Body"]
            raise KeyError(f"message not found: {msg_id}")

        return await asyncio.to_thread(_receive)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        msg_id = ref.location.rsplit("/", 1)[-1]
        self._message_cache.pop(msg_id, None)

        def _delete():
            client = self._get_client()
            resp = client.receive_message(
                QueueUrl=self._queue_url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=0,
            )
            for msg in resp.get("Messages", []):
                if msg["MessageId"] == msg_id:
                    client.delete_message(
                        QueueUrl=self._queue_url,
                        ReceiptHandle=msg["ReceiptHandle"],
                    )
                    return True
            return False

        try:
            return await asyncio.to_thread(_delete)
        except Exception:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        prefix = f"{manifest_id}:"
        return [
            StoredChunkRef(adapter_name="aws-sqs", location=f"sqs://{self._queue_url}/{mid}")
            for mid, data in self._message_cache.items()
            if data.startswith(f"PCHK:1:{manifest_id}:")
        ]

    async def health_check(self) -> bool:
        def _check():
            client = self._get_client()
            client.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            return True

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    async def capacity(self) -> dict[str, int | None]:
        def _capacity():
            client = self._get_client()
            attrs = client.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
            count = int(attrs["Attributes"].get("ApproximateNumberOfMessages", 0))
            return count

        try:
            count = await asyncio.to_thread(_capacity)
        except Exception:
            count = None
        return {
            "used_bytes": None,
            "free_bytes": None,
            "total_bytes": None,
            "chunk_count": count,
        }
