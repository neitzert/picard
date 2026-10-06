"""Tests for the Backblaze B2 adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.backblaze_b2 import BackblazeB2Adapter


@pytest.fixture
def adapter():
    return BackblazeB2Adapter(
        application_key_id="test-key-id",
        application_key="test-key",
        bucket_name="test-bucket",
    )


@pytest.fixture
def mock_auth():
    return {
        "authorizationToken": "tok_test",
        "apiUrl": "https://api001.backblazeb2.com",
        "downloadUrl": "https://f001.backblazeb2.com",
        "allowed": {"bucketId": "bucket123"},
    }


def _make_mock_resp(json_data=None, text_data=None, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if text_data is not None:
        resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_session(get_resp=None, post_resp=None):
    session = AsyncMock()
    if get_resp:
        session.get = MagicMock(return_value=get_resp)
    if post_resp:
        if isinstance(post_resp, list):
            session.post = MagicMock(side_effect=post_resp)
        else:
            session.post = MagicMock(return_value=post_resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "backblaze-b2"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 500


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter, mock_auth):
        adapter._auth = mock_auth
        upload_resp = _make_mock_resp(json_data={
            "uploadUrl": "https://upload.example.com",
            "authorizationToken": "upload_tok",
        })
        file_resp = _make_mock_resp(json_data={"fileId": "file123"})
        session = _make_mock_session(post_resp=[upload_resp, file_resp])

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("PCHK:test123:0:0:data")
            assert ref.adapter_name == "backblaze-b2"
            assert ref.location.endswith(".pchk")

    async def test_read_returns_data(self, adapter, mock_auth):
        adapter._auth = mock_auth
        resp = _make_mock_resp(text_data="PCHK:test123:0:0:data")
        session = _make_mock_session(get_resp=resp)

        with patch("aiohttp.ClientSession", return_value=session):
            from picard.adapters.base import StoredChunkRef

            ref = StoredChunkRef(adapter_name="backblaze-b2", location="picard/test.pchk")
            result = await adapter.read_chunk(ref)
            assert result == "PCHK:test123:0:0:data"

    async def test_write_opaque_data(self, adapter, mock_auth):
        adapter._auth = mock_auth
        upload_resp = _make_mock_resp(json_data={
            "uploadUrl": "https://upload.example.com",
            "authorizationToken": "upload_tok",
        })
        file_resp = _make_mock_resp(json_data={"fileId": "file456"})
        session = _make_mock_session(post_resp=[upload_resp, file_resp])

        with patch("aiohttp.ClientSession", return_value=session):
            ref = await adapter.write_chunk("deadbeef0123")
            assert ref.adapter_name == "backblaze-b2"


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter, mock_auth):
        adapter._auth = mock_auth
        resp = _make_mock_resp(status=200)
        session = _make_mock_session(post_resp=resp)

        with patch("aiohttp.ClientSession", return_value=session):
            from picard.adapters.base import StoredChunkRef

            ref = StoredChunkRef(adapter_name="backblaze-b2", location="picard/test.pchk")
            assert await adapter.delete_chunk(ref) is True


@pytest.mark.asyncio
class TestList:
    async def test_list_returns_refs(self, adapter, mock_auth):
        adapter._auth = mock_auth
        resp = _make_mock_resp(json_data={
            "files": [
                {"fileName": "picard/abc123/b0_c0.pchk"},
                {"fileName": "picard/abc123/b0_c1.pchk"},
            ]
        })
        session = _make_mock_session(post_resp=resp)

        with patch("aiohttp.ClientSession", return_value=session):
            refs = await adapter.list_chunks("abc123")
            assert len(refs) == 2
            assert all(r.adapter_name == "backblaze-b2" for r in refs)


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter):
        auth_resp = _make_mock_resp(json_data={
            "authorizationToken": "tok",
            "apiUrl": "https://api.example.com",
            "downloadUrl": "https://dl.example.com",
            "allowed": {"bucketId": "b123"},
        })
        session = _make_mock_session(get_resp=auth_resp)

        with patch("aiohttp.ClientSession", return_value=session):
            adapter._auth = None
            assert await adapter.health_check() is True

    async def test_health_fail(self, adapter):
        adapter._auth = None
        with patch("aiohttp.ClientSession", side_effect=Exception("no network")):
            assert await adapter.health_check() is False
