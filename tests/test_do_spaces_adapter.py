"""Tests for the DigitalOcean Spaces adapter -- mocked, no real DO calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.do_spaces import DoSpacesAdapter
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


def _make_adapter(**kwargs) -> DoSpacesAdapter:
    defaults = dict(
        access_key="test-key",
        secret_key="test-secret",
        space_name="test-space",
    )
    defaults.update(kwargs)
    return DoSpacesAdapter(**defaults)


async def _run_sync(fn, *args):
    """Replacement for asyncio.to_thread that runs synchronously."""
    return fn(*args)


@pytest.mark.asyncio
class TestDoSpacesAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "do-spaces"
        assert p.max_chunk_bytes == 5_368_709_120
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 1000

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "picard/m1/b0_c2.pchk"

    async def test_key_custom_prefix(self):
        a = _make_adapter(prefix="data/")
        assert a._key("abc", 1, 3) == "data/abc/b1_c3.pchk"

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_write_chunk(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "do-spaces"
        assert ref.location == "picard/m1/b0_c0.pchk"
        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-space"
        assert call_kwargs["Key"] == "picard/m1/b0_c0.pchk"

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_read_chunk(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        enc = _sample_encoded()

        body_mock = MagicMock()
        body_mock.read.return_value = enc.encode()
        s3.get_object.return_value = {"Body": body_mock}

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/m1/b0_c0.pchk")
        result = await a.read_chunk(ref)

        assert result == enc

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_delete_chunk_success(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is True
        s3.delete_object.assert_called_once()

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        s3.delete_object.side_effect = Exception("access denied")

        ref = StoredChunkRef(adapter_name="do-spaces", location="picard/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_list_chunks(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "picard/m1/b0_c0.pchk"},
                {"Key": "picard/m1/b0_c1.pchk"},
            ],
            "IsTruncated": False,
        }

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "do-spaces" for r in refs)

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_list_chunks_empty(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        s3.list_objects_v2.return_value = {"IsTruncated": False}

        refs = await a.list_chunks("m1")
        assert refs == []

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_health_check_ok(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        assert await a.health_check() is True
        s3.head_bucket.assert_called_once_with(Bucket="test-space")

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_health_check_fail(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        s3.head_bucket.side_effect = Exception("not found")

        assert await a.health_check() is False

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_write_chunk_fallback_name(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "do-spaces"
        assert ref.location.startswith("picard/")
        assert ref.location.endswith(".pchk")

    @patch("asyncio.to_thread", new=_run_sync)
    async def test_list_chunks_pagination(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        s3.list_objects_v2.side_effect = [
            {
                "Contents": [{"Key": "picard/m1/b0_c0.pchk"}],
                "IsTruncated": True,
                "NextContinuationToken": "tok1",
            },
            {
                "Contents": [{"Key": "picard/m1/b0_c1.pchk"}],
                "IsTruncated": False,
            },
        ]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert s3.list_objects_v2.call_count == 2
