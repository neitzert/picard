"""Tests for the CouchDB adapter -- mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.couchdb import CouchDbAdapter
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


def _make_adapter(**kwargs) -> CouchDbAdapter:
    defaults = dict(url="http://couch:5984", database="picard")
    defaults.update(kwargs)
    return CouchDbAdapter(**defaults)


def _mock_response(status_code: int = 200, json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestCouchDbAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "couchdb"
        assert p.max_chunk_bytes == 5_000_000
        assert p.persistent is True
        assert p.rate_limit_per_min == 500

    async def test_doc_id_format(self):
        a = _make_adapter()
        assert a._doc_id("m1", 0, 2) == "pchk_m1_b0_c2"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        a._db_ensured = True
        enc = _sample_encoded()

        client.put.return_value = _mock_response(
            201, json_data={"id": "pchk_m1_b0_c0", "rev": "1-abc"}
        )

        ref = await a.write_chunk(enc)
        assert ref.adapter_name == "couchdb"
        assert ref.location == "pchk_m1_b0_c0"

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response(
            200, json_data={"_id": "pchk_m1_b0_c0", "_rev": "1-abc", "data": enc}
        )

        ref = StoredChunkRef(adapter_name="couchdb", location="pchk_m1_b0_c0")
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(
            200, json_data={"_id": "pchk_m1_b0_c0", "_rev": "2-def"}
        )
        client.delete.return_value = _mock_response(200)

        ref = StoredChunkRef(adapter_name="couchdb", location="pchk_m1_b0_c0")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(404)

        ref = StoredChunkRef(adapter_name="couchdb", location="pchk_m1_b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("connection lost")

        ref = StoredChunkRef(adapter_name="couchdb", location="pchk_m1_b0_c0")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(
            200,
            json_data={
                "rows": [
                    {"id": "pchk_m1_b0_c0"},
                    {"id": "pchk_m1_b0_c1"},
                ]
            },
        )

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "couchdb" for r in refs)

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(200, json_data={"rows": []})
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_db_missing(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(404)
        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(
            200, json_data={"couchdb": "Welcome"}
        )
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("refused")
        assert await a.health_check() is False

    async def test_ensure_db_creates(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.put.return_value = _mock_response(201)

        await a._ensure_db()
        assert a._db_ensured is True
        client.put.assert_awaited_once()
