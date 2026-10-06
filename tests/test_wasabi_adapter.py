"""Tests for the Wasabi adapter."""

from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.wasabi_adapter import WasabiAdapter


@pytest.fixture
def adapter():
    return WasabiAdapter(
        access_key="test-key",
        secret_key="test-secret",
        bucket="test-bucket",
    )


@pytest.fixture
def mock_s3():
    client = MagicMock()
    return client


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "wasabi"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 1000


@pytest.mark.asyncio
class TestWriteRead:
    async def test_write_returns_ref(self, adapter, mock_s3):
        adapter._client = mock_s3
        ref = await adapter.write_chunk("PCHK:1:test123:0:0:1:ZGF0YTE=")
        assert ref.adapter_name == "wasabi"
        assert "test123" in ref.location
        mock_s3.put_object.assert_called_once()

    async def test_read_returns_data(self, adapter, mock_s3):
        body_mock = MagicMock()
        body_mock.read.return_value = b"PCHK:1:test123:0:0:1:ZGF0YTE="
        mock_s3.get_object.return_value = {"Body": body_mock}
        adapter._client = mock_s3

        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="wasabi", location="picard/test.pchk")
        result = await adapter.read_chunk(ref)
        assert result == "PCHK:1:test123:0:0:1:ZGF0YTE="

    async def test_write_opaque_data(self, adapter, mock_s3):
        adapter._client = mock_s3
        ref = await adapter.write_chunk("deadbeef0123")
        assert ref.adapter_name == "wasabi"
        assert ".pchk" in ref.location


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_success(self, adapter, mock_s3):
        adapter._client = mock_s3
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="wasabi", location="picard/test.pchk")
        assert await adapter.delete_chunk(ref) is True

    async def test_delete_failure(self, adapter, mock_s3):
        mock_s3.delete_object.side_effect = Exception("not found")
        adapter._client = mock_s3
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(adapter_name="wasabi", location="picard/nope.pchk")
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
        assert all(r.adapter_name == "wasabi" for r in refs)


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, adapter, mock_s3):
        adapter._client = mock_s3
        assert await adapter.health_check() is True

    async def test_health_fail(self, adapter, mock_s3):
        mock_s3.head_bucket.side_effect = Exception("access denied")
        adapter._client = mock_s3
        assert await adapter.health_check() is False
