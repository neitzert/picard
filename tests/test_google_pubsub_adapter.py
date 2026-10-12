"""Tests for Google Cloud Pub/Sub adapter — uses in-memory mock."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from picard.adapters.google_pubsub import GooglePubSubAdapter


class _FakeResponse:
    def __init__(self, data=None, status=200):
        self._data = data or {}
        self.status = status

    def raise_for_status(self):
        pass

    async def json(self):
        return self._data


class _FakeSession:
    def __init__(self):
        self._counter = 0
        self._messages: dict[str, str] = {}

    async def post(self, url, **kwargs):
        if ":publish" in url:
            body = kwargs.get("json", {})
            msgs = body.get("messages", [])
            ids = []
            for msg in msgs:
                self._counter += 1
                mid = f"msg_{self._counter}"
                self._messages[mid] = msg.get("data", "")
                ids.append(mid)
            return _FakeResponse({"messageIds": ids})
        if ":pull" in url:
            received = []
            for mid, data in self._messages.items():
                received.append({
                    "message": {"messageId": mid, "data": data},
                    "ackId": f"ack_{mid}",
                })
            return _FakeResponse({"receivedMessages": received})
        return _FakeResponse({})

    async def get(self, url, **kwargs):
        return _FakeResponse({"name": "test-topic"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


@pytest.fixture
def adapter():
    return GooglePubSubAdapter(
        project_id="test-project",
        topic_id="test-topic",
        access_token="test-token",
        subscription_id="test-sub",
    )


@pytest.fixture
def fake():
    return _FakeSession()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "google-pubsub"

    def test_not_persistent(self, adapter):
        assert adapter.profile().persistent is False

    def test_rate(self, adapter):
        assert adapter.profile().rate_limit_per_min == 600


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_roundtrip_via_cache(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("pubsubdata")
            assert ref.adapter_name == "google-pubsub"
            assert "pubsub://" in ref.location
            result = await adapter.read_chunk(ref)
            assert result == "pubsubdata"

    @pytest.mark.asyncio
    async def test_read_no_sub_raises(self, adapter, fake):
        adapter_no_sub = GooglePubSubAdapter(
            project_id="test-project",
            topic_id="test-topic",
            access_token="test-token",
        )
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter_no_sub.write_chunk("subdata")
            adapter_no_sub._cache.clear()
            with pytest.raises(KeyError):
                await adapter_no_sub.read_chunk(ref)


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            ref = await adapter.write_chunk("gone")
            assert await adapter.delete_chunk(ref) is True
            assert await adapter.delete_chunk(ref) is False


class TestList:
    @pytest.mark.asyncio
    async def test_list(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            await adapter.write_chunk("d1")
            chunks = await adapter.list_chunks("pubsub")
            assert len(chunks) >= 1


class TestHealth:
    @pytest.mark.asyncio
    async def test_healthy(self, adapter, fake):
        with patch("aiohttp.ClientSession", return_value=fake):
            assert await adapter.health_check() is True


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
