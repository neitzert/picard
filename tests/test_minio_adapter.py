"""Tests for the MinIO adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.minio_adapter import MinioAdapter
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


def _make_adapter(**kwargs) -> MinioAdapter:
    defaults = dict(
        endpoint="minio.example.com:9000",
        access_key="AKIATEST",
        secret_key="secret",
    )
    defaults.update(kwargs)
    return MinioAdapter(**defaults)


@pytest.mark.asyncio
class TestMinioAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "minio"
        assert p.max_chunk_bytes == 5_368_709_120
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 5000

    async def test_key_format(self):
        a = _make_adapter()
        assert a._key("m1", 0, 2) == "chunks/m1/b0_c2.pchk"

    async def test_key_custom_prefix(self):
        a = _make_adapter(prefix="data/objs/")
        assert a._key("abc", 1, 3) == "data/objs/abc/b1_c3.pchk"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "minio"
        assert ref.location == "chunks/m1/b0_c0.pchk"
        client.put_object.assert_called_once()
        call_args = client.put_object.call_args
        assert call_args[0][0] == "picard"
        assert call_args[0][1] == "chunks/m1/b0_c0.pchk"

    async def test_write_chunk_fallback_uuid(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        ref = await a.write_chunk("not-a-valid-chunk")

        assert ref.adapter_name == "minio"
        assert ref.location.startswith("chunks/")
        assert ref.location.endswith(".pchk")
        client.put_object.assert_called_once()

    async def test_read_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        enc = _sample_encoded()

        response_mock = MagicMock()
        response_mock.read.return_value = enc.encode()
        response_mock.close = MagicMock()
        response_mock.release_conn = MagicMock()
        client.get_object.return_value = response_mock

        ref = StoredChunkRef(adapter_name="minio", location="chunks/m1/b0_c0.pchk")
        result = await a.read_chunk(ref)

        assert result == enc
        client.get_object.assert_called_once_with("picard", "chunks/m1/b0_c0.pchk")

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        ref = StoredChunkRef(adapter_name="minio", location="chunks/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is True
        client.remove_object.assert_called_once_with("picard", "chunks/m1/b0_c0.pchk")

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.remove_object.side_effect = Exception("access denied")

        ref = StoredChunkRef(adapter_name="minio", location="chunks/m1/b0_c0.pchk")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        obj1 = MagicMock()
        obj1.object_name = "chunks/m1/b0_c0.pchk"
        obj2 = MagicMock()
        obj2.object_name = "chunks/m1/b0_c1.pchk"
        client.list_objects.return_value = [obj1, obj2]

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "minio" for r in refs)
        assert refs[0].location == "chunks/m1/b0_c0.pchk"
        client.list_objects.assert_called_once_with("picard", prefix="chunks/m1/")

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.list_objects.return_value = []

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.bucket_exists.return_value = True

        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client
        client.bucket_exists.side_effect = Exception("connection refused")

        assert await a.health_check() is False

    async def test_capacity(self):
        a = _make_adapter()
        client = MagicMock()
        a._client = client

        obj1 = MagicMock()
        obj1.size = 100
        obj2 = MagicMock()
        obj2.size = 200
        client.list_objects.return_value = [obj1, obj2]

        cap = await a.capacity()
        assert cap["used_bytes"] == 300
        assert cap["chunk_count"] == 2
        assert cap["free_bytes"] is None
        assert cap["total_bytes"] is None
