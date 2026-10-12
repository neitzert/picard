"""Tests for the CardDAV adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.carddav import (
    CarddavAdapter,
    _build_vcard,
    _extract_note,
    _extract_uid,
)
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


def _make_adapter(**kwargs) -> CarddavAdapter:
    defaults = dict(
        url="https://dav.example.com",
        username="user",
        password="pass",
        addressbook_path="addressbooks/picard/",
    )
    defaults.update(kwargs)
    return CarddavAdapter(**defaults)


def _mock_response(text: str = "", status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


class TestHelpers:
    def test_build_vcard(self):
        vc = _build_vcard("abc123", "m1", 0, 0, "chunk-data-here")
        assert "UID:abc123@picard" in vc
        assert "FN:PCHK m1 b0 c0" in vc
        assert "NOTE:chunk-data-here" in vc
        assert vc.startswith("BEGIN:VCARD")

    def test_extract_note(self):
        vc = _build_vcard("x", "m1", 0, 0, "my-note-value")
        assert _extract_note(vc) == "my-note-value"

    def test_extract_note_missing(self):
        assert _extract_note("BEGIN:VCARD\r\nEND:VCARD") is None

    def test_extract_uid(self):
        vc = _build_vcard("abc123", "m1", 0, 0, "data")
        assert _extract_uid(vc) == "abc123"


@pytest.mark.asyncio
class TestCarddavAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "carddav"
        assert p.max_chunk_bytes == 50_000
        assert p.persistent is True

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.put.return_value = _mock_response(status_code=201)

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "carddav"
        assert len(ref.location) == 16
        client.put.assert_awaited_once()
        call_args = client.put.call_args
        assert call_args.kwargs["headers"]["Content-Type"].startswith("text/vcard")

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        vcard = _build_vcard("msg1", "m1", 0, 0, enc)
        client.get.return_value = _mock_response(text=vcard)

        ref = StoredChunkRef(adapter_name="carddav", location="msg1")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_chunk_no_note(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(text="BEGIN:VCARD\r\nEND:VCARD")

        ref = StoredChunkRef(adapter_name="carddav", location="msg1")
        with pytest.raises(ValueError, match="no NOTE field"):
            await a.read_chunk(ref)

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.request.return_value = _mock_response(status_code=204)
        ref = StoredChunkRef(adapter_name="carddav", location="msg1")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.side_effect = Exception("connection error")

        ref = StoredChunkRef(adapter_name="carddav", location="msg1")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        xml_body = (
            '<?xml version="1.0"?>'
            "<multistatus>"
            "<response><propstat><prop>"
            "<address-data>"
            "BEGIN:VCARD\r\nUID:aaa111@picard\r\n"
            "FN:PCHK m1 b0 c0\r\nNOTE:data1\r\nEND:VCARD"
            "</address-data>"
            "</prop></propstat></response>"
            "<response><propstat><prop>"
            "<address-data>"
            "BEGIN:VCARD\r\nUID:bbb222@picard\r\n"
            "FN:PCHK m1 b0 c1\r\nNOTE:data2\r\nEND:VCARD"
            "</address-data>"
            "</prop></propstat></response>"
            "</multistatus>"
        )

        client.request.return_value = _mock_response(text=xml_body, status_code=207)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        locs = {r.location for r in refs}
        assert "aaa111" in locs
        assert "bbb222" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.request.return_value = _mock_response(
            text='<?xml version="1.0"?><multistatus></multistatus>',
            status_code=207,
        )
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.return_value = _mock_response(status_code=207)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.return_value = _mock_response(status_code=401)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.side_effect = Exception("timeout")
        assert await a.health_check() is False
