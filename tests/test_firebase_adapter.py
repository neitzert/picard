"""Tests for the Firebase Realtime Database adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.firebase_adapter import FirebaseAdapter


@pytest.fixture
def adapter():
    return FirebaseAdapter(
        database_url="https://test-project.firebaseio.com",
        auth_token="test-token",
    )


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "firebase"

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
        assert ref.adapter_name == "firebase"
        assert "firebase://" in ref.location

    async def test_read_returns_data(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"data": "PCHK:1:abc:0:0:data"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="firebase", location="firebase://picard/chunks/abc_0_0")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data"

    async def test_read_missing_raises(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=None)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="firebase", location="firebase://picard/chunks/nope")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_resp)
        adapter._client = mock_client
        ref = StoredChunkRef(adapter_name="firebase", location="firebase://picard/chunks/abc_0_0")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    async def test_list_by_manifest(self, adapter):
        mock_client = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "abc_0_0": True,
            "abc_1_0": True,
            "other_0_0": True,
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
