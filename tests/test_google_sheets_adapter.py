"""Tests for the Google Sheets adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.google_sheets import GoogleSheetsAdapter


@pytest.fixture
def adapter():
    return GoogleSheetsAdapter(
        credentials_json='{"type":"service_account","project_id":"test"}',
        spreadsheet_id="sheet123",
        sheet_name="picard",
    )


@pytest.fixture
def mock_service():
    svc = MagicMock()
    sheets = MagicMock()
    values = MagicMock()
    svc.spreadsheets.return_value = sheets
    sheets.values.return_value = values
    sheets.get.return_value.execute.return_value = {"spreadsheetId": "sheet123"}
    return svc, values


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "google-sheets"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 50000

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter, mock_service):
        svc, values = mock_service
        values.append.return_value.execute.return_value = {}
        adapter._service = svc
        ref = await adapter.write_chunk("PCHK:1:abc:0:0:data")
        assert ref.adapter_name == "google-sheets"
        assert "gsheets://" in ref.location
        values.append.assert_called_once()

    async def test_read_finds_chunk(self, adapter, mock_service):
        svc, values = mock_service
        values.get.return_value.execute.return_value = {
            "values": [["abc:0:0", "PCHK:1:abc:0:0:data"]]
        }
        adapter._service = svc
        ref = StoredChunkRef(adapter_name="google-sheets", location="gsheets://sheet123/picard!abc:0:0")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:abc:0:0:data"

    async def test_read_missing_raises(self, adapter, mock_service):
        svc, values = mock_service
        values.get.return_value.execute.return_value = {"values": []}
        adapter._service = svc
        ref = StoredChunkRef(adapter_name="google-sheets", location="gsheets://sheet123/picard!nope")
        with pytest.raises(KeyError):
            await adapter.read_chunk(ref)


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter, mock_service):
        svc, values = mock_service
        values.get.return_value.execute.return_value = {
            "values": [["abc:0:0", "data"]]
        }
        values.update.return_value.execute.return_value = {}
        adapter._service = svc
        ref = StoredChunkRef(adapter_name="google-sheets", location="gsheets://sheet123/picard!abc:0:0")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_missing(self, adapter, mock_service):
        svc, values = mock_service
        values.get.return_value.execute.return_value = {"values": []}
        adapter._service = svc
        ref = StoredChunkRef(adapter_name="google-sheets", location="gsheets://sheet123/picard!nope")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    async def test_list_by_manifest(self, adapter, mock_service):
        svc, values = mock_service
        values.get.return_value.execute.return_value = {
            "values": [["abc:0:0"], ["abc:1:0"], ["other:0:0"]]
        }
        adapter._service = svc
        refs = await adapter.list_chunks("abc")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter, mock_service):
        svc, _ = mock_service
        adapter._service = svc
        assert await adapter.health_check() is True

    async def test_unhealthy(self, adapter):
        adapter._service = MagicMock()
        adapter._service.spreadsheets.return_value.get.return_value.execute.side_effect = Exception("fail")
        assert await adapter.health_check() is False
