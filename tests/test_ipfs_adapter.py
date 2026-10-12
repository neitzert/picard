"""Tests for the IPFS adapter — mocked, no network calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.ipfs import IpfsAdapter
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


def _make_adapter(**kwargs) -> IpfsAdapter:
    defaults = dict(api_url="http://localhost:5001")
    defaults.update(kwargs)
    return IpfsAdapter(**defaults)


def _mock_response(json_data=None, text=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if text is not None:
        resp.text = text
    return resp


@pytest.mark.asyncio
class TestIpfsAdapter:
    async def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "ipfs"
        assert p.max_chunk_bytes == 250_000
        assert p.persistent is True
        assert p.ordered is False

    async def test_profile_no_pin(self):
        a = _make_adapter(pin=False)
        p = a.profile()
        assert p.persistent is False

    async def test_write_chunk_with_pin(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response(
            json_data={"Name": "chunk.txt", "Hash": "QmTestCid123", "Size": "100"}
        )

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "ipfs"
        assert ref.location == "QmTestCid123"
        assert client.post.await_count == 4  # add + pin + mkdir + cp

    async def test_write_chunk_no_pin(self):
        a = _make_adapter(pin=False)
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response(
            json_data={"Name": "chunk.txt", "Hash": "QmNoPinCid", "Size": "50"}
        )

        ref = await a.write_chunk(enc)

        assert ref.location == "QmNoPinCid"
        assert client.post.await_count == 3  # add + mkdir + cp (no pin)

    async def test_read_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded()

        client.post.return_value = _mock_response(text=enc)

        ref = StoredChunkRef(adapter_name="ipfs", location="QmTestCid123")
        result = await a.read_chunk(ref)

        assert result == enc
        client.post.assert_awaited_once_with(
            "/api/v0/cat", params={"arg": "QmTestCid123"}
        )

    async def test_delete_chunk(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded(manifest_id="m1")

        unpin_resp = _mock_response(json_data={"Pins": ["QmTestCid123"]})
        cat_resp = _mock_response(text=enc)
        rm_resp = _mock_response(json_data={})
        client.post.side_effect = [unpin_resp, cat_resp, rm_resp]

        ref = StoredChunkRef(adapter_name="ipfs", location="QmTestCid123")
        assert await a.delete_chunk(ref) is True

    async def test_delete_chunk_failure(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("connection refused")

        ref = StoredChunkRef(adapter_name="ipfs", location="QmBadCid")
        assert await a.delete_chunk(ref) is False

    async def test_delete_chunk_no_pin(self):
        a = _make_adapter(pin=False)
        client = AsyncMock()
        a._client = client
        enc = _sample_encoded(manifest_id="m1")

        cat_resp = _mock_response(text=enc)
        rm_resp = _mock_response(json_data={})
        client.post.side_effect = [cat_resp, rm_resp]

        ref = StoredChunkRef(adapter_name="ipfs", location="QmTestCid")
        assert await a.delete_chunk(ref) is True
        assert client.post.await_count == 2  # cat + files/rm (no unpin)

    async def test_list_chunks(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response(json_data={
            "Entries": [
                {"Name": "QmCid1", "Size": 100, "Type": 0},
                {"Name": "QmCid2", "Size": 200, "Type": 0},
            ]
        })

        refs = await a.list_chunks("m1")

        assert len(refs) == 2
        assert all(r.adapter_name == "ipfs" for r in refs)
        assert refs[0].location == "QmCid1"
        assert refs[1].location == "QmCid2"

    async def test_list_chunks_empty(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response(json_data={"Entries": None})

        refs = await a.list_chunks("m1")
        assert refs == []

    async def test_list_chunks_no_directory(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("file does not exist")

        refs = await a.list_chunks("nonexistent")
        assert refs == []

    async def test_health_check_ok(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response(
            json_data={"ID": "QmPeerID123", "AgentVersion": "kubo/0.20.0"},
            status_code=200,
        )
        assert await a.health_check() is True

    async def test_health_check_fail(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client

        client.post.return_value = _mock_response(json_data={}, status_code=500)
        assert await a.health_check() is False

    async def test_health_check_exception(self):
        a = _make_adapter()
        client = AsyncMock()
        a._client = client
        client.post.side_effect = Exception("timeout")
        assert await a.health_check() is False
