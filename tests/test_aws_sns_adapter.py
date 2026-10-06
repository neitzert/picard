"""Tests for AWS SNS adapter — uses in-memory mock."""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = type("ClientSession", (), {})
sys.modules.setdefault("aiohttp", _aiohttp)

from picard.adapters.aws_sns import AwsSnsAdapter


class _FakeResponse:
    def __init__(self, text="", status=200):
        self._text = text
        self.status = status

    def raise_for_status(self):
        pass

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self):
        self._counter = 0

    async def post(self, url, **kwargs):
        self._counter += 1
        return _FakeResponse(
            text=f"<PublishResponse><MessageId>msg_{self._counter}</MessageId></PublishResponse>"
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return AwsSnsAdapter(
        topic_arn="arn:aws:sns:us-east-1:123456789:test-topic",
        access_key_id="AKIATEST",
        secret_access_key="testsecret",
    )


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "aws-sns"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate(self, adapter):
        assert adapter.profile().rate_limit_per_min == 300


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_and_cache_read(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("snsdata")
            assert ref.adapter_name == "aws-sns"
            assert "sns://" in ref.location
            result = await adapter.read_chunk(ref)
            assert result == "snsdata"

    @pytest.mark.asyncio
    async def test_read_miss_raises(self, adapter):
        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(adapter_name="aws-sns", location="sns://missing/123")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("bye")
            assert await adapter.delete_chunk(ref) is True
            assert await adapter.delete_chunk(ref) is False


class TestList:
    @pytest.mark.asyncio
    async def test_list(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("d1")
            chunks = await adapter.list_chunks("sns")
            assert len(chunks) >= 1


class TestSignV4:
    def test_sign_produces_headers(self, adapter):
        from datetime import datetime, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        headers = adapter._sign_v4("POST", "https://sns.us-east-1.amazonaws.com/", "body", now)
        assert "Authorization" in headers
        assert "AWS4-HMAC-SHA256" in headers["Authorization"]
        assert headers["x-amz-date"] == "20260101T000000Z"


class TestCapacity:
    @pytest.mark.asyncio
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0

    @pytest.mark.asyncio
    async def test_with_data(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("payload")
            cap = await adapter.capacity()
            assert cap["chunk_count"] == 1
