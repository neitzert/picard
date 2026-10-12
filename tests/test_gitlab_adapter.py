"""Tests for the GitLab snippets adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.gitlab import GitLabAdapter
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


def _make_adapter(**kwargs) -> GitLabAdapter:
    defaults = dict(token="glpat-xxxx")
    defaults.update(kwargs)
    return GitLabAdapter(**defaults)


def _mock_response(json_data=None, text: str = "", status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
class TestGitLabAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "gitlab"
        assert p.max_chunk_bytes == 900_000
        assert p.persistent is True
        assert p.ordered is False
        assert p.rate_limit_per_min == 60

    async def test_snippet_title(self):
        assert GitLabAdapter._snippet_title("m1", 0, 2) == "pchk_m1_b0_c2"
        assert GitLabAdapter._snippet_title("abc", 3, 7) == "pchk_abc_b3_c7"

    async def test_api_url(self):
        a = _make_adapter()
        assert a._api == "https://gitlab.com/api/v4"

    async def test_custom_base_url(self):
        a = _make_adapter(base_url="https://git.corp.io/")
        assert a._api == "https://git.corp.io/api/v4"

    async def test_write_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response({"id": 42})

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "gitlab"
        assert ref.location == "42"
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs["json"]
        assert body["title"] == "pchk_m1_b0_c0"
        assert body["visibility"] == "private"
        assert body["files"][0]["content"] == enc

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        resp = _mock_response(text=enc)
        client.get.return_value = resp

        ref = StoredChunkRef(adapter_name="gitlab", location="42")
        result = await a.read_chunk(ref)

        assert result == enc
        client.get.assert_awaited_once_with(
            "https://gitlab.com/api/v4/snippets/42/raw"
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(status_code=204)

        ref = StoredChunkRef(adapter_name="gitlab", location="42")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.delete.return_value = _mock_response(status_code=404)

        ref = StoredChunkRef(adapter_name="gitlab", location="42")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.delete.side_effect = Exception("network error")

        ref = StoredChunkRef(adapter_name="gitlab", location="42")
        assert await a.delete_chunk(ref) is False

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        page1 = _mock_response([
            {"id": 10, "title": "pchk_m1_b0_c0"},
            {"id": 11, "title": "pchk_m1_b0_c1"},
            {"id": 12, "title": "pchk_m2_b0_c0"},
        ])
        page2 = _mock_response([])
        client.get.side_effect = [page1, page2]

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        locs = {r.location for r in refs}
        assert locs == {"10", "11"}

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

        client.get.return_value = _mock_response(status_code=200)
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.get.return_value = _mock_response(status_code=401)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.get.side_effect = Exception("timeout")
        assert await a.health_check() is False
