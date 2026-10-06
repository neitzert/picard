"""Tests for the Cloudflare R2 adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.cloudflare_r2 import CloudflareR2Adapter


@pytest.fixture
def adapter():
    return CloudflareR2Adapter(
        account_id="test-account",
        access_key_id="AKTEST",
        secret_access_key="secret123",
        bucket="test-bucket",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "cloudflare-r2"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 5_368_709_120

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.put = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            ref = await adapter.write_chunk("test-chunk-data")

        assert ref.adapter_name == "cloudflare-r2"
        assert ref.location.startswith("r2://test-bucket/")

    async def test_read_returns_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=b"chunk-content")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="cloudflare-r2",
            location="r2://test-bucket/chunks/abc/b0_c0.pchk",
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await adapter.read_chunk(ref)

        assert result == "chunk-content"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 204
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.delete = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="cloudflare-r2",
            location="r2://test-bucket/chunks/abc/b0_c0.pchk",
        )
        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_parses_xml(self, adapter):
        xml_body = (
            '<?xml version="1.0"?>'
            "<ListBucketResult>"
            "<Contents><Key>chunks/mid1/b0_c0.pchk</Key></Contents>"
            "<Contents><Key>chunks/mid1/b0_c1.pchk</Key></Contents>"
            "</ListBucketResult>"
        )
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value=xml_body)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            refs = await adapter.list_chunks("mid1")

        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_healthy(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.head = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=Exception("nope"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            assert await adapter.health_check() is False
