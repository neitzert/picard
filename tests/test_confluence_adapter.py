"""Tests for the Confluence adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.confluence_adapter import ConfluenceAdapter


@pytest.fixture
def adapter():
    return ConfluenceAdapter(
        server_url="https://test.atlassian.net",
        email="test@example.com",
        api_token="tok123",
        space_key="PCR",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "confluence"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"id": "12345"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = await adapter.write_chunk("PCHK:1:abc:0:0:data")
        assert ref.adapter_name == "confluence"
        assert "12345" in ref.location

    async def test_read_extracts_cdata(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "body": {
                "storage": {
                    "value": "<ac:structured-macro><ac:plain-text-body><![CDATA[PCHK:1:abc:0:0:data]]></ac:plain-text-body></ac:structured-macro>"
                }
            }
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="confluence", location="confluence://PCR/12345")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data"

    async def test_read_no_cdata_raises(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "body": {"storage": {"value": "<p>no data here</p>"}}
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="confluence", location="confluence://PCR/99999")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 204
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="confluence", location="confluence://PCR/12345")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    async def test_list_by_manifest(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "results": [{"id": "p1"}, {"id": "p2"}]
        })
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
