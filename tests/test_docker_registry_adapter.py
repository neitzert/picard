"""Tests for the Docker/OCI registry adapter."""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.docker_registry import DockerRegistryAdapter


@pytest.fixture
def adapter():
    return DockerRegistryAdapter(
        "https://registry.example.com",
        repository="picard/chunks",
        username="user",
        password="pass",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "docker-registry"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 10_485_760

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 120


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_oci_location(self, adapter):
        chunk_data = "PCHK:test123:0:0:hello"
        digest = f"sha256:{hashlib.sha256(chunk_data.encode()).hexdigest()}"

        mock_post_resp = AsyncMock()
        mock_post_resp.raise_for_status = MagicMock()
        mock_post_resp.headers = {"Location": "/v2/picard/chunks/blobs/uploads/uuid123"}
        mock_post_resp.__aenter__ = AsyncMock(return_value=mock_post_resp)
        mock_post_resp.__aexit__ = AsyncMock(return_value=False)

        mock_put_resp = AsyncMock()
        mock_put_resp.raise_for_status = MagicMock()
        mock_put_resp.__aenter__ = AsyncMock(return_value=mock_put_resp)
        mock_put_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_post_resp)
        mock_session.put = MagicMock(return_value=mock_put_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk(chunk_data)

        assert ref.adapter_name == "docker-registry"
        assert "oci://" in ref.location
        assert digest in ref.location

    async def test_read_returns_data(self, adapter):
        chunk_data = "PCHK:test123:0:0:hello"

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=chunk_data.encode())
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="docker-registry",
            location="oci://registry.example.com/picard/chunks@sha256:abc123",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == chunk_data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 202
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.delete = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="docker-registry",
            location="oci://registry.example.com/picard/chunks@sha256:abc123",
        )

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("conn refused"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_returns_none_values(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] is None
        assert cap["chunk_count"] is None
