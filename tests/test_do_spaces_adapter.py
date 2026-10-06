"""Tests for the DigitalOcean Spaces adapter."""

from unittest.mock import MagicMock

import pytest

from picard.adapters.do_spaces import DoSpacesAdapter


@pytest.fixture
def adapter():
    return DoSpacesAdapter(
        access_key="test-key",
        secret_key="test-secret",
        space_name="test-space",
    )


@pytest.fixture
def mock_s3():
    return MagicMock()


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "do-spaces"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter, mock_s3):
        adapter._client = mock_s3
        ref = await adapter.write_chunk("PCHK:test123:0:0:data")
        assert ref.adapter_name == "do-spaces"
        assert ref.location.endswith(".pchk")
        mock_s3.put_object.assert_called_once()

    async def test_read_returns_data(self, adapter, mock_s3):
        body_mock = MagicMock()
        body_mock.read.return_value = b"PCHK:test123:0:0:data"
        mock_s3.get_object.return_value = {"Body": body_mock}
        adapter._client = mock_s3

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/test.pchk")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:test123:0:0:data"

    async def test_write_opaque_data(self, adapter, mock_s3):
        adapter._client = mock_s3
        ref = await adapter.write_chunk("sealed_opaque_data")
        assert ref.adapter_name == "do-spaces"
        assert ".pchk" in ref.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter, mock_s3):
        adapter._client = mock_s3
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/test.pchk")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter, mock_s3):
        mock_s3.delete_object.side_effect = Exception("no")
        adapter._client = mock_s3
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/nope.pchk")
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestList:
    async def test_list_returns_refs(self, adapter, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "picard/abc123/b0_c0.pchk"},
                {"Key": "picard/abc123/b0_c1.pchk"},
            ],
            "IsTruncated": False,
        }
        adapter._client = mock_s3
        refs = await adapter.list_chunks("abc123")
        assert len(refs) == 2
        assert all(r.adapter_name == "do-spaces" for r in refs)


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter, mock_s3):
        adapter._client = mock_s3
        assert await adapter.health_check() is True

    async def test_health_fail(self, adapter, mock_s3):
        mock_s3.head_bucket.side_effect = Exception("down")
        adapter._client = mock_s3
        assert await adapter.health_check() is False
