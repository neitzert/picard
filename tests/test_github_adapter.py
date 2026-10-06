"""Tests for the GitHub Gist adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.github import (
    GitHubAdapter,
    _pack_location,
    _unpack_location,
)
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


def _make_adapter(**kwargs) -> GitHubAdapter:
    defaults = dict(token="ghp_test_token_xxx")
    defaults.update(kwargs)
    return GitHubAdapter(**defaults)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


class TestLocationPacking:
    def test_pack(self):
        loc = _pack_location("gist123", "pchk_m1_b0_c0.txt")
        assert loc == "gist123:pchk_m1_b0_c0.txt"

    def test_unpack(self):
        gist_id, fname = _unpack_location("gist123:pchk_m1_b0_c0.txt")
        assert gist_id == "gist123"
        assert fname == "pchk_m1_b0_c0.txt"

    def test_round_trip(self):
        loc = _pack_location("abc", "file.txt")
        assert _unpack_location(loc) == ("abc", "file.txt")


@pytest.mark.asyncio
class TestGitHubAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "github"
        assert p.max_chunk_bytes == 900_000
        assert p.persistent is True
        assert p.ordered is False

    async def test_chunk_filename(self):
        assert GitHubAdapter._chunk_filename("m1", 0, 2) == "pchk_m1_b0_c2.txt"
        assert GitHubAdapter._chunk_filename("abc", 3, 7) == "pchk_abc_b3_c7.txt"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({"id": "gist-new-123"}, 201)

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "github"
        gist_id, fname = _unpack_location(ref.location)
        assert gist_id == "gist-new-123"
        assert fname == "pchk_m1_b0_c0.txt"

        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["public"] is False
        assert body["description"] == "picard-chunks"
        assert body["files"]["pchk_m1_b0_c0.txt"]["content"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.get.return_value = _mock_response({
            "files": {"pchk_m1_b0_c0.txt": {"content": enc}},
        })

        ref = StoredChunkRef(
            adapter_name="github",
            location=_pack_location("gist123", "pchk_m1_b0_c0.txt"),
        )
        result = await a.read_chunk(ref)
        assert result == enc

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({}, 204)

        ref = StoredChunkRef(
            adapter_name="github",
            location=_pack_location("gist123", "pchk_m1_b0_c0.txt"),
        )
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response({}, 404)

        ref = StoredChunkRef(
            adapter_name="github",
            location=_pack_location("gist123", "pchk_m1_b0_c0.txt"),
        )
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("network error")

        ref = StoredChunkRef(
            adapter_name="github",
            location=_pack_location("gist123", "pchk_m1_b0_c0.txt"),
        )
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        page1 = _mock_response([
            {
                "id": "g1",
                "description": "picard-chunks",
                "files": {
                    "pchk_m1_b0_c0.txt": {},
                    "pchk_m1_b0_c1.txt": {},
                },
            },
            {
                "id": "g2",
                "description": "picard-chunks",
                "files": {"pchk_m2_b0_c0.txt": {}},
            },
        ])
        page2 = _mock_response([])
        client.get.side_effect = [page1, page2]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        locs = [_unpack_location(r.location) for r in refs]
        assert ("g1", "pchk_m1_b0_c0.txt") in locs
        assert ("g1", "pchk_m1_b0_c1.txt") in locs

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response([])

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"login": "testuser"}, 200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response({"message": "Bad credentials"}, 401)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False
