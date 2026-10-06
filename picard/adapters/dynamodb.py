"""DynamoDB adapter — stores chunks as items in a DynamoDB table.

Each chunk becomes an item with composite key (pk=manifest, sk=block/chunk).

Requires: pip install picard[dynamodb]  (boto3)
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


def _pack_location(pk: str, sk: str) -> str:
    return f"{pk}|{sk}"


def _unpack_location(location: str) -> tuple[str, str]:
    pk, sk = location.split("|", 1)
    return pk, sk


class DynamoDbAdapter(Adapter):
    """Scatter chunks into DynamoDB items."""

    def __init__(
        self,
        *,
        table_name: str = "picard-chunks",
        region: str = "us-east-1",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        max_chunk_bytes: int = 400_000,
    ) -> None:
        self._table_name = table_name
        self._region = region
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._max = max_chunk_bytes
        self._table: object | None = None

    def _ensure_table(self):
        if self._table is not None:
            return self._table
        import boto3

        kwargs: dict = {"region_name": self._region}
        if self._access_key:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_key
        dynamodb = boto3.resource("dynamodb", **kwargs)
        self._table = dynamodb.Table(self._table_name)
        return self._table

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="dynamodb",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=2000,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        table = self._ensure_table()
        chunk = Chunk.decode(encoded_chunk)
        pk = f"PCHK#{chunk.manifest_id}"
        sk = f"b{chunk.block_index}#c{chunk.chunk_index}"

        def _put():
            table.put_item(Item={"pk": pk, "sk": sk, "data": encoded_chunk})

        await asyncio.to_thread(_put)
        return StoredChunkRef(
            adapter_name="dynamodb", location=_pack_location(pk, sk)
        )

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        table = self._ensure_table()
        pk, sk = _unpack_location(ref.location)

        def _get():
            resp = table.get_item(Key={"pk": pk, "sk": sk})
            item = resp.get("Item")
            if item is None:
                raise ValueError(f"chunk not found: {ref.location}")
            return item["data"]

        return await asyncio.to_thread(_get)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        table = self._ensure_table()
        pk, sk = _unpack_location(ref.location)

        def _delete():
            try:
                table.delete_item(Key={"pk": pk, "sk": sk})
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete)

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        table = self._ensure_table()
        pk = f"PCHK#{manifest_id}"

        def _query():
            from boto3.dynamodb.conditions import Key

            resp = table.query(
                KeyConditionExpression=Key("pk").eq(pk),
            )
            return [
                StoredChunkRef(
                    adapter_name="dynamodb",
                    location=_pack_location(item["pk"], item["sk"]),
                )
                for item in resp.get("Items", [])
            ]

        return await asyncio.to_thread(_query)

    async def health_check(self) -> bool:
        try:
            table = self._ensure_table()
            await asyncio.to_thread(lambda: table.table_status)
            return True
        except Exception:
            return False
