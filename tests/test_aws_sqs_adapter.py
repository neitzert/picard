"""Tests for the AWS SQS adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.aws_sqs import AwsSqsAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter():
    return AwsSqsAdapter(
        queue_url="https://sqs.us-east-1.amazonaws.com/123456/picard",
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "aws-sqs"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 3000

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 262_144


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {"MessageId": "msg-001"}
        with patch.object(adapter, "_get_client", return_value=mock_client):
            ref = await adapter.write_chunk("PCHK:1:abc:0:0:data")
        assert ref.adapter_name == "aws-sqs"
        assert "msg-001" in ref.location

    async def test_read_from_cache(self, adapter):
        adapter._message_cache["msg-001"] = "PCHK:1:abc:0:0:data"
        ref = StoredChunkRef(adapter_name="aws-sqs", location="sqs://queue/msg-001")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data"

    async def test_read_from_sqs(self, adapter):
        mock_client = MagicMock()
        mock_client.receive_message.return_value = {
            "Messages": [
                {"MessageId": "msg-002", "Body": "PCHK:1:abc:0:0:data2"}
            ]
        }
        with patch.object(adapter, "_get_client", return_value=mock_client):
            ref = StoredChunkRef(adapter_name="aws-sqs", location="sqs://queue/msg-002")
            result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data2"

    async def test_read_missing_raises(self, adapter):
        mock_client = MagicMock()
        mock_client.receive_message.return_value = {"Messages": []}
        with patch.object(adapter, "_get_client", return_value=mock_client):
            ref = StoredChunkRef(adapter_name="aws-sqs", location="sqs://queue/nope")
            with pytest.raises(KeyError):
                await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_found(self, adapter):
        adapter._message_cache["msg-001"] = "data"
        mock_client = MagicMock()
        mock_client.receive_message.return_value = {
            "Messages": [{"MessageId": "msg-001", "ReceiptHandle": "rh-001"}]
        }
        mock_client.delete_message.return_value = {}
        with patch.object(adapter, "_get_client", return_value=mock_client):
            ref = StoredChunkRef(adapter_name="aws-sqs", location="sqs://queue/msg-001")
            assert await adapter.delete_chunk(ref) is True
        assert "msg-001" not in adapter._message_cache


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_client = MagicMock()
        mock_client.get_queue_attributes.return_value = {
            "Attributes": {"ApproximateNumberOfMessages": "5"}
        }
        with patch.object(adapter, "_get_client", return_value=mock_client):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_client = MagicMock()
        mock_client.get_queue_attributes.side_effect = Exception("fail")
        with patch.object(adapter, "_get_client", return_value=mock_client):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity(self, adapter):
        mock_client = MagicMock()
        mock_client.get_queue_attributes.return_value = {
            "Attributes": {"ApproximateNumberOfMessages": "42"}
        }
        with patch.object(adapter, "_get_client", return_value=mock_client):
            cap = await adapter.capacity()
        assert cap["chunk_count"] == 42
