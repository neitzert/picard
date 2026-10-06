"""Tests for the Google Calendar adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.calendar_adapter import GoogleCalendarAdapter


@pytest.fixture
def adapter(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    return GoogleCalendarAdapter(credentials_json=str(creds))


@pytest.mark.asyncio
class TestProfile:
    async def test_name(self, adapter):
        assert adapter.profile().name == "google-calendar"

    async def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    async def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60

    async def test_max_chunk(self, adapter):
        assert adapter.profile().max_chunk_bytes == 8192


@pytest.mark.asyncio
class TestWriteRead:
    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_round_trip(self, mock_svc, adapter):
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {"id": "event123"}
        mock_events.insert.return_value = mock_insert
        mock_svc.return_value.events.return_value = mock_events

        ref = await adapter.write_chunk("calendar-data")
        assert ref.adapter_name == "google-calendar"
        assert "gcal://" in ref.location

        result = await adapter.read_chunk(ref)
        assert result == "calendar-data"

    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_read_from_api(self, mock_svc, adapter):
        mock_events = MagicMock()
        mock_get = MagicMock()
        mock_get.execute.return_value = {"description": "api-data"}
        mock_events.get.return_value = mock_get
        mock_svc.return_value.events.return_value = mock_events

        from picard.adapters.base import StoredChunkRef
        ref = StoredChunkRef(
            adapter_name="google-calendar",
            location="gcal://primary/event999",
        )
        result = await adapter.read_chunk(ref)
        assert result == "api-data"


@pytest.mark.asyncio
class TestDelete:
    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_delete(self, mock_svc, adapter):
        mock_events = MagicMock()
        mock_insert = MagicMock()
        mock_insert.execute.return_value = {"id": "del1"}
        mock_events.insert.return_value = mock_insert
        mock_delete = MagicMock()
        mock_delete.execute.return_value = None
        mock_events.delete.return_value = mock_delete
        mock_svc.return_value.events.return_value = mock_events

        ref = await adapter.write_chunk("del-cal")
        assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_list(self, mock_svc, adapter):
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [{"id": "e1"}, {"id": "e2"}]
        }
        mock_events.list.return_value = mock_list
        mock_svc.return_value.events.return_value = mock_events

        refs = await adapter.list_chunks("man1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["used_bytes"] == 0
        assert cap["chunk_count"] == 0


@pytest.mark.asyncio
class TestHealthCheck:
    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_healthy(self, mock_svc, adapter):
        mock_cal_list = MagicMock()
        mock_cal_list_exec = MagicMock()
        mock_cal_list_exec.execute.return_value = {"items": []}
        mock_cal_list.list.return_value = mock_cal_list_exec
        mock_svc.return_value.calendarList.return_value = mock_cal_list

        assert await adapter.health_check() is True

    @patch("picard.adapters.calendar_adapter.GoogleCalendarAdapter._get_service")
    async def test_unhealthy(self, mock_svc, adapter):
        mock_svc.side_effect = Exception("no auth")
        assert await adapter.health_check() is False
