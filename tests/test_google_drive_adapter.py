"""Tests for the Google Drive adapter -- mocked, no real API calls."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.google_drive import GoogleDriveAdapter
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


def _make_adapter(**kwargs) -> GoogleDriveAdapter:
    defaults = dict(
        credentials_json=None,
        folder_id="test-folder",
        prefix="picard-",
    )
    defaults.update(kwargs)
    return GoogleDriveAdapter(**defaults)


def _mock_response(status=200, json_data=None, text_data=""):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp):
    session = AsyncMock()
    session.post = MagicMock(return_value=resp)
    session.get = MagicMock(return_value=resp)
    session.delete = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.mark.asyncio
class TestGoogleDriveAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "google-drive"
        assert p.max_chunk_bytes == 5_242_880
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 60

    async def test_profile_custom_max(self):
        a = _make_adapter(max_chunk_bytes=1_000_000)
        p = a.profile()
        assert p.max_chunk_bytes == 1_000_000

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, json_data={"id": "file-abc123"})
        session = _mock_session(resp)

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "google-drive"
        assert ref.location == "file-abc123"
        session.post.assert_called_once()

    async def test_write_chunk_no_folder(self):
        a = _make_adapter(folder_id=None)
        enc = _sample_encoded()
        resp = _mock_response(200, json_data={"id": "file-xyz"})
        session = _mock_session(resp)

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer tok"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            ref = await a.write_chunk(enc)

        assert ref.location == "file-xyz"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        resp = _mock_response(200, text_data=enc)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="google-drive", location="file-abc123")

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_success(self):
        a = _make_adapter()
        resp = _mock_response(204)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="google-drive", location="file-abc123")

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.delete_chunk(ref)

        assert result is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.delete = MagicMock(side_effect=Exception("network error"))

        ref = StoredChunkRef(adapter_name="google-drive", location="file-abc123")

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.delete_chunk(ref)

        assert result is False

    async def test_list_chunks(self):
        a = _make_adapter()
        resp = _mock_response(200, json_data={
            "files": [
                {"id": "f1", "name": "picard-m1_b0_c0.pchk"},
                {"id": "f2", "name": "picard-m1_b0_c1.pchk"},
            ]
        })
        session = _mock_session(resp)

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "google-drive" for r in refs)
        assert refs[0].location == "f1"
        assert refs[1].location == "f2"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        resp = _mock_response(200, json_data={"files": []})
        session = _mock_session(resp)

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            refs = await a.list_chunks("nonexistent")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        resp = _mock_response(200)
        session = _mock_session(resp)

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.health_check()

        assert result is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(side_effect=Exception("network error"))

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.health_check()

        assert result is False

    async def test_delete_chunk_200(self):
        a = _make_adapter()
        resp = _mock_response(200)
        session = _mock_session(resp)

        ref = StoredChunkRef(adapter_name="google-drive", location="file-abc123")

        with patch("picard.adapters.google_drive.aiohttp.ClientSession", return_value=session), \
             patch.object(a, "_get_headers", return_value={"Authorization": "Bearer test-token"}), \
             patch("picard.adapters.google_drive.asyncio.to_thread", side_effect=_fake_to_thread):
            result = await a.delete_chunk(ref)

        assert result is True
