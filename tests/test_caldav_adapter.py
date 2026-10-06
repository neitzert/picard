"""Tests for the CalDAV adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.caldav import CalDavAdapter, _build_ical, _parse_description
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1,
        manifest_id=manifest_id,
        block_index=block,
        chunk_index=idx,
        total_chunks=1,
        payload=b"test-payload",
    ).encode()


def _make_adapter(**kwargs) -> CalDavAdapter:
    defaults = dict(
        url="https://cal.example.com/dav",
        username="user",
        password="pass",
    )
    defaults.update(kwargs)
    return CalDavAdapter(**defaults)


def _mock_response(status_code: int = 200, text: str = "", json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    return resp


class TestIcalHelpers:
    def test_build_ical(self):
        ical = _build_ical("abc123", "m1", 0, 0, "PCHK:data")
        assert "UID:abc123@picard" in ical
        assert "SUMMARY:PCHK m1 b0 c0" in ical
        assert "DESCRIPTION:PCHK:data" in ical
        assert "BEGIN:VCALENDAR" in ical
        assert "END:VCALENDAR" in ical

    def test_parse_description(self):
        ical = _build_ical("x", "m1", 0, 0, "payload-here")
        assert _parse_description(ical) == "payload-here"

    def test_parse_description_missing(self):
        with pytest.raises(ValueError, match="no DESCRIPTION"):
            _parse_description("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")


@pytest.mark.asyncio
class TestCalDavAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "caldav"
        assert p.max_chunk_bytes == 50_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 60

    async def test_cal_url(self):
        a = _make_adapter()
        assert a._cal_url() == "https://cal.example.com/dav/calendars/picard"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.put.return_value = _mock_response(201)

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "caldav"
        assert len(ref.location) == 16

        call = client.put.call_args
        url = call[0][0]
        assert url.endswith(".ics")
        assert "calendars/picard" in url
        body = call[1]["content"]
        assert "DESCRIPTION:" in body
        assert enc in body

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        ical = _build_ical("abc", "m1", 0, 0, enc)
        client.get.return_value = _mock_response(200, text=ical)

        ref = StoredChunkRef(adapter_name="caldav", location="abc")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.return_value = _mock_response(204)

        ref = StoredChunkRef(adapter_name="caldav", location="abc")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.return_value = _mock_response(404)

        ref = StoredChunkRef(adapter_name="caldav", location="abc")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("timeout")

        ref = StoredChunkRef(adapter_name="caldav", location="abc")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        propfind_xml = (
            '<?xml version="1.0"?>'
            '<d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/dav/calendars/picard/evt1.ics</d:href></d:response>"
            "<d:response><d:href>/dav/calendars/picard/evt2.ics</d:href></d:response>"
            "<d:response><d:href>/dav/calendars/picard/</d:href></d:response>"
            "</d:multistatus>"
        )
        enc = _sample_encoded()
        ical_match = _build_ical("evt1", "m1", 0, 0, enc)
        ical_other = _build_ical("evt2", "m2", 0, 0, enc.replace("m1", "m2"))

        client.request.return_value = _mock_response(207, text=propfind_xml)
        client.get.side_effect = [
            _mock_response(200, text=ical_match),
            _mock_response(200, text=ical_other),
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 1
        assert refs[0].location == "evt1"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        propfind_xml = (
            '<?xml version="1.0"?>'
            '<d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/dav/calendars/picard/</d:href></d:response>"
            "</d:multistatus>"
        )
        client.request.return_value = _mock_response(207, text=propfind_xml)

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.return_value = _mock_response(207)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.return_value = _mock_response(401)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.side_effect = Exception("timeout")
        assert await a.health_check() is False
