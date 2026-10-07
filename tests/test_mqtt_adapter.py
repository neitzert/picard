"""Tests for the MQTT adapter -- mocked, no network calls."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.mqtt import MqttAdapter
from picard.chunker import Chunk


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> MqttAdapter:
    defaults = dict(broker_host="localhost")
    defaults.update(kwargs)
    return MqttAdapter(**defaults)


def _mock_client() -> MagicMock:
    client = MagicMock()
    info = MagicMock()
    info.wait_for_publish = MagicMock()
    client.publish.return_value = info
    return client


@pytest.mark.asyncio
class TestMqttAdapter:
    @pytest.fixture(autouse=True)
    def _patch_to_thread(self):
        with patch("picard.adapters.mqtt.asyncio.to_thread", _fake_to_thread):
            yield

    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "mqtt"
        assert p.max_chunk_bytes == 8192
        assert p.persistent is True
        assert p.ordered is False

    async def test_topic_format(self):
        topic = MqttAdapter._topic("manifest1", 2, 3)
        assert topic == "picard/manifest1/2/3"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "mqtt"
        assert ref.location == "picard/m1/0/0"
        client.publish.assert_called_once_with(
            "picard/m1/0/0", enc.encode("utf-8"), retain=True, qos=1
        )

    async def test_read_chunk(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client
        enc = _sample_encoded()

        ref = StoredChunkRef(adapter_name="mqtt", location="picard/m1/0/0")

        def fake_subscribe(topic, qos=1):
            pass

        def fake_callback_add(topic, callback):
            msg = MagicMock()
            msg.payload = enc.encode("utf-8")
            callback(None, None, msg)

        client.subscribe = MagicMock(side_effect=fake_subscribe)
        client.message_callback_add = MagicMock(side_effect=fake_callback_add)
        client.unsubscribe = MagicMock()
        client.message_callback_remove = MagicMock()

        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_timeout(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client

        ref = StoredChunkRef(adapter_name="mqtt", location="picard/m1/0/0")

        client.subscribe = MagicMock()
        client.message_callback_add = MagicMock()
        client.unsubscribe = MagicMock()
        client.message_callback_remove = MagicMock()

        orig_wait = threading.Event.wait

        def fast_wait(self, timeout=None):
            return orig_wait(self, timeout=0.01)

        with patch.object(threading.Event, "wait", fast_wait):
            with pytest.raises(TimeoutError, match="no retained message"):
                await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client

        ref = StoredChunkRef(adapter_name="mqtt", location="picard/m1/0/0")
        assert await a.delete_chunk(ref) is True
        client.publish.assert_called_once_with(
            "picard/m1/0/0", b"", retain=True, qos=1
        )

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client
        client.publish.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="mqtt", location="picard/m1/0/0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client

        enc0 = _sample_encoded("m1", 0, 0)
        enc1 = _sample_encoded("m1", 0, 1)

        def fake_callback_add(topic, callback):
            msg0 = MagicMock()
            msg0.payload = enc0.encode("utf-8")
            msg0.topic = "picard/m1/0/0"
            callback(None, None, msg0)

            msg1 = MagicMock()
            msg1.payload = enc1.encode("utf-8")
            msg1.topic = "picard/m1/0/1"
            callback(None, None, msg1)

        client.subscribe = MagicMock()
        client.message_callback_add = MagicMock(side_effect=fake_callback_add)
        client.unsubscribe = MagicMock()
        client.message_callback_remove = MagicMock()

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "mqtt" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = _mock_client()
        a._client = client

        client.subscribe = MagicMock()
        client.message_callback_add = MagicMock()
        client.unsubscribe = MagicMock()
        client.message_callback_remove = MagicMock()

        orig_wait = threading.Event.wait

        def fast_wait(self, timeout=None):
            return orig_wait(self, timeout=0.01)

        with patch.object(threading.Event, "wait", fast_wait):
            refs = await a.list_chunks("m1")
            assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        a._client = _mock_client()
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        with patch.object(a, "_ensure_client", side_effect=Exception("refused")):
            assert await a.health_check() is False
