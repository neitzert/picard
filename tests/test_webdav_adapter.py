"""Tests for the WebDAV adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.webdav import WebdavAdapter
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


def _make_adapter(**kwargs) -> WebdavAdapter:
    defaults = dict(
        url="https://dav.example.com",
        username="user",
        password="pass",
        base_path="/picard/",
    )
    defaults.update(kwargs)
    return WebdavAdapter(**defaults)


def _mock_response(text: str = "", status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestWebdavAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "webdav"
        assert p.max_chunk_bytes == 50_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 120

    async def test_full_url(self):
        a = _make_adapter()
        assert a._full_url("m1/b0_c0.pchk") == "https://dav.example.com/picard/m1/b0_c0.pchk"
        assert a._full_url() == "https://dav.example.com/picard/"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        # MKCOL for directory creation
        client.request.return_value = _mock_response(status_code=201)
        # PUT for the file
        client.put.return_value = _mock_response(status_code=201)

        enc = _sample_encoded()
        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "webdav"
        assert ref.location == "m1/b0_c0.pchk"
        client.request.assert_awaited_once()  # MKCOL
        client.put.assert_awaited_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response(text=enc)

        ref = StoredChunkRef(adapter_name="webdav", location="m1/b0_c0.pchk")
        result = await a.read_chunk(ref)
        assert result == enc
        client.get.assert_awaited_once_with(
            "https://dav.example.com/picard/m1/b0_c0.pchk"
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.request.return_value = _mock_response(status_code=204)
        ref = StoredChunkRef(adapter_name="webdav", location="m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.request.return_value = _mock_response(status_code=404)
        ref = StoredChunkRef(adapter_name="webdav", location="m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.request.side_effect = Exception("network error")

        ref = StoredChunkRef(adapter_name="webdav", location="m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        xml_body = (
            '<?xml version="1.0"?>'
            '<D:multistatus xmlns:D="DAV:">'
            "<D:response><D:href>/picard/m1/</D:href></D:response>"
            "<D:response><D:href>/picard/m1/b0_c0.pchk</D:href></D:response>"
            "<D:response><D:href>/picard/m1/b0_c1.pchk</D:href></D:response>"
            "</D:multistatus>"
        )
        client.request.return_value = _mock_response(text=xml_body, status_code=207)

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        locs = {r.location for r in refs}
        assert "m1/b0_c0.pchk" in locs
        assert "m1/b0_c1.pchk" in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        xml_body = (
            '<?xml version="1.0"?>'
            '<D:multistatus xmlns:D="DAV:">'
            "<D:response><D:href>/picard/m1/</D:href></D:response>"
            "</D:multistatus>"
        )
        client.request.return_value = _mock_response(text=xml_body, status_code=207)

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.request.return_value = _mock_response(status_code=404)
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

    async def test_base_path_trailing_slash(self):
        a = _make_adapter(base_path="/stuff")
        assert a._base_path == "/stuff/"
