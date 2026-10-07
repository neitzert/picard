"""Tests for the Backblaze B2 adapter -- mocked, no real B2 calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.backblaze_b2 import BackblazeB2Adapter
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


_FAKE_AUTH = {
    "authorizationToken": "fake-token",
    "apiUrl": "https://api001.backblazeb2.com",
    "downloadUrl": "https://f001.backblazeb2.com",
    "allowed": {"bucketId": "bucket123"},
}


def _make_adapter(**kwargs) -> BackblazeB2Adapter:
    defaults = dict(
        application_key_id="test-key-id",
        application_key="test-key",
        bucket_name="test-bucket",
    )
    defaults.update(kwargs)
    a = BackblazeB2Adapter(**defaults)
    a._auth = dict(_FAKE_AUTH)
    return a


def _mock_response(status=200, json_data=None, text_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    return resp


@pytest.mark.asyncio
class TestBackblazeB2Adapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "backblaze-b2"
        assert p.max_chunk_bytes == 5_368_709_120
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 500

    async def test_obj_name(self):
        a = _make_adapter()
        assert a._obj_name("m1", 0, 2) == "picard/m1/b0_c2.pchk"

    async def test_obj_name_custom_prefix(self):
        a = _make_adapter(prefix="data/")
        assert a._obj_name("abc", 1, 3) == "data/abc/b1_c3.pchk"

    async def test_write_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        upload_resp = _mock_response(json_data={"uploadUrl": "https://upload.example.com", "authorizationToken": "upload-tok"})
        upload_exec_resp = _mock_response()

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            post_ctx_1 = AsyncMock()
            post_ctx_1.__aenter__ = AsyncMock(return_value=upload_resp)
            post_ctx_1.__aexit__ = AsyncMock(return_value=False)

            post_ctx_2 = AsyncMock()
            post_ctx_2.__aenter__ = AsyncMock(return_value=upload_exec_resp)
            post_ctx_2.__aexit__ = AsyncMock(return_value=False)

            session.post = MagicMock(side_effect=[post_ctx_1, post_ctx_2])

            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "backblaze-b2"
        assert ref.location == "picard/m1/b0_c0.pchk"

    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()

        read_resp = _mock_response(text_data=enc)

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            get_ctx = AsyncMock()
            get_ctx.__aenter__ = AsyncMock(return_value=read_resp)
            get_ctx.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(return_value=get_ctx)

            ref = StoredChunkRef(adapter_name="backblaze-b2", location="picard/m1/b0_c0.pchk")
            result = await a.read_chunk(ref)

        assert result == enc

    async def test_delete_chunk_success(self):
        a = _make_adapter()

        del_resp = _mock_response(status=200)

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=del_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            ref = StoredChunkRef(adapter_name="backblaze-b2", location="picard/m1/b0_c0.pchk")
            assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_exception(self):
        a = _make_adapter()

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm
            session.post = MagicMock(side_effect=Exception("network error"))

            ref = StoredChunkRef(adapter_name="backblaze-b2", location="picard/m1/b0_c0.pchk")
            assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()

        list_resp = _mock_response(json_data={
            "files": [
                {"fileName": "picard/m1/b0_c0.pchk"},
                {"fileName": "picard/m1/b0_c1.pchk"},
            ]
        })

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=list_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "backblaze-b2" for r in refs)
        assert refs[0].location == "picard/m1/b0_c0.pchk"

    async def test_list_chunks_empty(self):
        a = _make_adapter()

        list_resp = _mock_response(json_data={"files": []})

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            post_ctx = AsyncMock()
            post_ctx.__aenter__ = AsyncMock(return_value=list_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=False)
            session.post = MagicMock(return_value=post_ctx)

            refs = await a.list_chunks("m1")

        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        a._auth = None  # force re-auth

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm
            session.get = MagicMock(side_effect=Exception("connection refused"))

            assert await a.health_check() is False

    async def test_authorize_caches(self):
        a = _make_adapter()
        result = await a._authorize()
        assert result == _FAKE_AUTH

    async def test_write_chunk_fallback_name(self):
        a = _make_adapter()

        upload_resp = _mock_response(json_data={"uploadUrl": "https://upload.example.com", "authorizationToken": "upload-tok"})
        upload_exec_resp = _mock_response()

        with patch("picard.adapters.backblaze_b2.aiohttp.ClientSession") as mock_session_cls:
            session = AsyncMock()
            cm = MagicMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = cm

            post_ctx_1 = AsyncMock()
            post_ctx_1.__aenter__ = AsyncMock(return_value=upload_resp)
            post_ctx_1.__aexit__ = AsyncMock(return_value=False)

            post_ctx_2 = AsyncMock()
            post_ctx_2.__aenter__ = AsyncMock(return_value=upload_exec_resp)
            post_ctx_2.__aexit__ = AsyncMock(return_value=False)

            session.post = MagicMock(side_effect=[post_ctx_1, post_ctx_2])

            ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "backblaze-b2"
        assert ref.location.startswith("picard/")
        assert ref.location.endswith(".pchk")
