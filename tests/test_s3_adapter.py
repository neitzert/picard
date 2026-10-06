"""Tests for the S3 adapter -- mocked, no AWS calls."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.s3 import S3Adapter
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


def _make_adapter(**kwargs) -> S3Adapter:
    defaults = dict(
        bucket="test-bucket",
        prefix="picard/",
        region="us-east-1",
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
    )
    defaults.update(kwargs)
    return S3Adapter(**defaults)


@pytest.mark.asyncio
class TestS3Adapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "s3"
        assert p.max_chunk_bytes == 5_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 3000

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "picard/m1/b0_c2.pchk"

    async def test_key_custom_prefix(self):
        a = _make_adapter(prefix="data/chunks/")
        assert a._key("abc", 1, 3) == "data/chunks/abc/b1_c3.pchk"

    async def test_write_chunk(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "s3"
        assert ref.location == "picard/m1/b0_c0.pchk"
        s3.put_object.assert_called_once()
        call_kwargs = s3.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "picard/m1/b0_c0.pchk"
        assert call_kwargs["Body"] == enc.encode()

    async def test_read_chunk(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        enc = _sample_encoded()

        body_mock = MagicMock()
        body_mock.read.return_value = enc.encode()
        s3.get_object.return_value = {"Body": body_mock}

        ref = StoredChunkRef(adapter_name="s3", location="picard/m1/b0_c0.pchk")
        result = await a.read_chunk(ref)

        assert result == enc
        s3.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="picard/m1/b0_c0.pchk"
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        ref = StoredChunkRef(adapter_name="s3", location="picard/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is True
        s3.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key="picard/m1/b0_c0.pchk"
        )

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        s3.delete_object.side_effect = Exception("access denied")

        ref = StoredChunkRef(adapter_name="s3", location="picard/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

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
        assert all(r.adapter_name == "s3" for r in refs)
        assert refs[0].location == "picard/m1/b0_c0.pchk"

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

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        s3.list_objects_v2.return_value = {"IsTruncated": False}

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3

        assert await a.health_check() is True
        s3.head_bucket.assert_called_once_with(Bucket="test-bucket")

    async def test_health_check_fail(self):
        a = _make_adapter()
        s3 = MagicMock()
        a._client = s3
        s3.head_bucket.side_effect = Exception("not found")

        assert await a.health_check() is False
