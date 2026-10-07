"""Tests for streaming put/get."""
import hashlib

import pytest

from picard.adapters.memory import MemoryAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator


async def _async_iter(chunks):
    """Helper: convert a list of bytes into an async iterator."""
    for c in chunks:
        yield c


class TestPutStream:
    @pytest.mark.asyncio
    async def test_stream_single_block(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=1024)
        data = b"hello streaming world"
        m = await orch.put_stream("/s1.txt", _async_iter([data]), "pass")
        assert m.original_size == len(data)
        assert m.total_blocks == 1
        result = await orch.get("/s1.txt", "pass")
        assert result == data

    @pytest.mark.asyncio
    async def test_stream_multiple_blocks(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        data = b"A" * 200
        m = await orch.put_stream("/multi.txt", _async_iter([data]), "pass")
        assert m.original_size == 200
        assert m.total_blocks == 4
        result = await orch.get("/multi.txt", "pass")
        assert result == data

    @pytest.mark.asyncio
    async def test_stream_chunked_input(self):
        """Input arrives in small chunks, not aligned to block_size."""
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        data = b"X" * 150
        chunks = [data[i : i + 10] for i in range(0, len(data), 10)]
        m = await orch.put_stream("/chunked.txt", _async_iter(chunks), "pass")
        assert m.original_size == 150
        result = await orch.get("/chunked.txt", "pass")
        assert result == data

    @pytest.mark.asyncio
    async def test_stream_empty(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index)
        m = await orch.put_stream("/empty.txt", _async_iter([]), "pass")
        assert m.original_size == 0
        assert m.total_blocks == 0

    @pytest.mark.asyncio
    async def test_stream_hash_matches_put(self):
        """put_stream and put produce the same hash for the same data."""
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        data = b"hash test data" * 10
        expected_hash = hashlib.sha256(data).hexdigest()
        m = await orch.put_stream("/hash.txt", _async_iter([data]), "pass")
        assert m.plaintext_hash == expected_hash

    @pytest.mark.asyncio
    async def test_stream_sets_version(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        m1 = await orch.put_stream("/ver.txt", _async_iter([b"v1"]), "pass")
        m2 = await orch.put_stream("/ver.txt", _async_iter([b"v2"]), "pass")
        assert m1.version == 1
        assert m2.version == 2


class TestGetStream:
    @pytest.mark.asyncio
    async def test_get_stream_yields_blocks(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        data = b"B" * 200
        await orch.put("/stream_get.txt", data, "pass")
        blocks = []
        async for block in orch.get_stream("/stream_get.txt", "pass"):
            blocks.append(block)
        assert len(blocks) == 4
        assert b"".join(blocks) == data

    @pytest.mark.asyncio
    async def test_get_stream_single_block(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=1024)
        data = b"small file"
        await orch.put("/small.txt", data, "pass")
        blocks = []
        async for block in orch.get_stream("/small.txt", "pass"):
            blocks.append(block)
        assert len(blocks) == 1
        assert blocks[0] == data

    @pytest.mark.asyncio
    async def test_get_stream_not_found(self):
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index)
        with pytest.raises(FileNotFoundError):
            async for _ in orch.get_stream("/nope.txt", "pass"):
                pass

    @pytest.mark.asyncio
    async def test_roundtrip_stream_to_stream(self):
        """put_stream -> get_stream roundtrip."""
        adapter = MemoryAdapter()
        index = Index(":memory:")
        orch = Orchestrator([adapter], index, block_size=64)
        data = b"C" * 300
        await orch.put_stream("/rt.txt", _async_iter([data]), "pass")
        result = bytearray()
        async for block in orch.get_stream("/rt.txt", "pass"):
            result.extend(block)
        assert bytes(result) == data
