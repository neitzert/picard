"""Tests for DockerLabelsAdapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.docker_labels import DockerLabelsAdapter


@pytest.fixture
def adapter():
    return DockerLabelsAdapter(image_prefix="test/chunk")


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "docker-labels"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65_536


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_container = AsyncMock()
        mock_container.id = "abc123def456"
        mock_docker = AsyncMock()
        mock_docker.containers.create_or_replace = AsyncMock(return_value=mock_container)
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            ref = await adapter.write_chunk("opaque-data")
        assert ref.adapter_name == "docker-labels"
        assert "abc123def456" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_container = AsyncMock()
        mock_container.show = AsyncMock(return_value={
            "Config": {"Labels": {"picard.data": "my-chunk"}}
        })
        mock_docker = AsyncMock()
        mock_docker.containers.get = AsyncMock(return_value=mock_container)
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            ref = MagicMock()
            ref.location = "docker://abc123def456/labels/picard.data"
            data = await adapter.read_chunk(ref)
        assert data == "my-chunk"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_container = AsyncMock()
        mock_container.delete = AsyncMock()
        mock_docker = AsyncMock()
        mock_docker.containers.get = AsyncMock(return_value=mock_container)
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            ref = MagicMock()
            ref.location = "docker://abc123/labels/picard.data"
            assert await adapter.delete_chunk(ref) is True

    async def test_delete_fail(self, adapter):
        mock_docker = AsyncMock()
        mock_docker.containers.get = AsyncMock(side_effect=Exception("not found"))
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            ref = MagicMock()
            ref.location = "docker://nope/labels/picard.data"
            assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list(self, adapter):
        c1 = {"Id": "aaa111"}
        c2 = {"Id": "bbb222"}
        mock_docker = AsyncMock()
        mock_docker.containers.list = AsyncMock(return_value=[c1, c2])
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            refs = await adapter.list_chunks("test-manifest")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_docker = AsyncMock()
        mock_docker.ping = AsyncMock()
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_docker = AsyncMock()
        mock_docker.ping = AsyncMock(side_effect=Exception("nope"))
        mock_docker.close = AsyncMock()
        with patch.object(adapter, "_get_client", return_value=mock_docker):
            assert await adapter.health_check() is False
