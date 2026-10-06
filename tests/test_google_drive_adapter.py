"""Tests for the Google Drive adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.google_drive import GoogleDriveAdapter


@pytest.fixture
def adapter():
    return GoogleDriveAdapter(credentials_json=None)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "google-drive"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"id": "file123"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch.object(adapter, "_get_headers", return_value={}):
                ref = await adapter.write_chunk("PCHK:test123:0:0:data")
                assert ref.adapter_name == "google-drive"
                assert ref.location == "file123"

    async def test_read_returns_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = AsyncMock(return_value="PCHK:test123:0:0:data")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch.object(adapter, "_get_headers", return_value={}):
                from picard.adapters.base import StoredChunkRef

                ref = StoredChunkRef(adapter_name="google-drive", location="file123")
                result = await adapter.read_chunk(ref)
                assert result == "PCHK:test123:0:0:data"

    async def test_write_opaque_data(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"id": "sealed_id"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch.object(adapter, "_get_headers", return_value={}):
                ref = await adapter.write_chunk("deadbeef0123")
                assert ref.adapter_name == "google-drive"


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

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch.object(adapter, "_get_headers", return_value={}):
                from picard.adapters.base import StoredChunkRef

                ref = StoredChunkRef(adapter_name="google-drive", location="file123")
                assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    async def test_list_returns_refs(self, adapter):
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "files": [
                {"id": "f1", "name": "picard-abc123_b0_c0.pchk"},
                {"id": "f2", "name": "picard-abc123_b0_c1.pchk"},
            ]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch.object(adapter, "_get_headers", return_value={}):
                refs = await adapter.list_chunks("abc123")
                assert len(refs) == 2
                assert all(r.adapter_name == "google-drive" for r in refs)


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
            with patch.object(adapter, "_get_headers", return_value={}):
                assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        with patch("aiohttp.ClientSession", side_effect=Exception("no")):
            with patch.object(adapter, "_get_headers", return_value={}):
                assert await adapter.health_check() is False
