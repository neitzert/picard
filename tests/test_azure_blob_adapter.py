"""Tests for the Azure Blob Storage adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.azure_blob import AzureBlobAdapter
from picard.adapters.base import StoredChunkRef


@pytest.fixture
def adapter():
    return AzureBlobAdapter(
        account_url="https://teststorage.blob.core.windows.net",
        sas_token="sv=2021&sig=test",
        container="picard",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "azure-blob"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = await adapter.write_chunk("PCHK:1:abc:0:0:data")
        assert ref.adapter_name == "azure-blob"
        assert "azblob://" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=b"PCHK:1:abc:0:0:data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="azure-blob", location="azblob://picard/abc/0_0.pchk")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 202
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="azure-blob", location="azblob://picard/abc/0_0.pchk")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    async def test_list_chunks(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.text = AsyncMock(return_value=(
            "<EnumerationResults>"
            "<Blobs><Blob><Name>abc/0_0.pchk</Name></Blob>"
            "<Blob><Name>abc/1_0.pchk</Name></Blob></Blobs>"
            "</EnumerationResults>"
        ))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        refs = await adapter.list_chunks("abc")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        mock_client = AsyncMock()
        mock_client.get = MagicMock(side_effect=Exception("fail"))
        adapter._client = mock_client
        assert await adapter.health_check() is False
