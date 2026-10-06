"""Tests for K8sConfigMapAdapter."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _setup_k8s_mock():
    mock_client = MagicMock()
    mock_config = MagicMock()
    mock_config.load_kube_config = AsyncMock()
    mock_config.load_incluster_config = MagicMock(
        side_effect=type("ConfigException", (Exception,), {})
    )
    mock_config.ConfigException = type("ConfigException", (Exception,), {})
    mock_k8s = MagicMock()
    mock_k8s.client = mock_client
    mock_k8s.config = mock_config
    sys.modules["kubernetes_asyncio"] = mock_k8s
    sys.modules["kubernetes_asyncio.client"] = mock_client
    sys.modules["kubernetes_asyncio.config"] = mock_config
    return mock_client, mock_config


_mock_client, _mock_config = _setup_k8s_mock()

from picard.adapters.k8s_configmap import K8sConfigMapAdapter


@pytest.fixture
def adapter():
    return K8sConfigMapAdapter(namespace="test-ns")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "k8s-configmap"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        api = AsyncMock()
        api.create_namespaced_config_map = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            ref = await adapter.write_chunk("opaque-data")
        assert ref.adapter_name == "k8s-configmap"
        assert "configmap" in ref.location
        api.create_namespaced_config_map.assert_awaited_once()

    async def test_read_returns_data(self, adapter):
        api = AsyncMock()
        cm = MagicMock()
        cm.data = {"chunk": "test-data"}
        api.read_namespaced_config_map = AsyncMock(return_value=cm)
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/configmap/picard-abc123"
            data = await adapter.read_chunk(ref)
        assert data == "test-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        api = AsyncMock()
        api.delete_namespaced_config_map = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/configmap/picard-abc"
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_fail(self, adapter):
        api = AsyncMock()
        api.delete_namespaced_config_map = AsyncMock(side_effect=Exception("nope"))
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/configmap/picard-abc"
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        api = AsyncMock()
        item = MagicMock()
        item.metadata.name = "picard-abc-b0-c0"
        resp = MagicMock()
        resp.items = [item]
        api.list_namespaced_config_map = AsyncMock(return_value=resp)
        with patch.object(adapter, "_get_api", return_value=api):
            refs = await adapter.list_chunks("abc")
        assert len(refs) == 1


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        api = AsyncMock()
        api.list_namespaced_config_map = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch.object(adapter, "_get_api", side_effect=Exception("fail")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity(self, adapter):
        api = AsyncMock()
        item = MagicMock()
        item.data = {"chunk": "hello"}
        resp = MagicMock()
        resp.items = [item]
        api.list_namespaced_config_map = AsyncMock(return_value=resp)
        with patch.object(adapter, "_get_api", return_value=api):
            cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == 5
