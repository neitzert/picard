"""Tests for K8sSecretAdapter."""

import base64
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _setup_k8s_mock():
    """Set up kubernetes_asyncio mock before importing the adapter."""
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

from picard.adapters.k8s_secret import K8sSecretAdapter


@pytest.fixture
def adapter():
    return K8sSecretAdapter(namespace="test-ns")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "k8s-secret"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 1_048_576


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        api = AsyncMock()
        api.create_namespaced_secret = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            ref = await adapter.write_chunk("opaque-sealed-data")
        assert ref.adapter_name == "k8s-secret"
        assert "test-ns" in ref.location
        assert "secret" in ref.location
        api.create_namespaced_secret.assert_awaited_once()

    async def test_read_returns_data(self, adapter):
        api = AsyncMock()
        secret = MagicMock()
        secret.data = {"chunk": base64.b64encode(b"test-data").decode()}
        api.read_namespaced_secret = AsyncMock(return_value=secret)
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/secret/picard-abc123"
            data = await adapter.read_chunk(ref)
        assert data == "test-data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        api = AsyncMock()
        api.delete_namespaced_secret = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/secret/picard-abc123"
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_not_found(self, adapter):
        api = AsyncMock()
        api.delete_namespaced_secret = AsyncMock(side_effect=Exception("404"))
        with patch.object(adapter, "_get_api", return_value=api):
            ref = MagicMock()
            ref.location = "k8s://test-ns/secret/picard-nope"
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        api = AsyncMock()
        item1 = MagicMock()
        item1.metadata.name = "picard-abc-b0-c0"
        item2 = MagicMock()
        item2.metadata.name = "picard-abc-b0-c1"
        resp = MagicMock()
        resp.items = [item1, item2]
        api.list_namespaced_secret = AsyncMock(return_value=resp)
        with patch.object(adapter, "_get_api", return_value=api):
            refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        api = AsyncMock()
        api.list_namespace = AsyncMock()
        with patch.object(adapter, "_get_api", return_value=api):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        with patch.object(adapter, "_get_api", side_effect=Exception("fail")):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_capacity_with_chunks(self, adapter):
        api = AsyncMock()
        item = MagicMock()
        item.data = {"chunk": base64.b64encode(b"hello").decode()}
        resp = MagicMock()
        resp.items = [item]
        api.list_namespaced_secret = AsyncMock(return_value=resp)
        with patch.object(adapter, "_get_api", return_value=api):
            cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] == 5
