"""Integration tests: full orchestrator round-trips using only free, no-login adapters.

Covers FilesystemAdapter, SQLiteLocalAdapter, and MemoryAdapter in:
  - single-adapter round-trips
  - multi-adapter scatter (chunks land on multiple backends)
  - persistence across orchestrator restarts
  - streaming put/get
  - verify, rm, cp, mv, mkdir/tree
  - binary payloads
  - wrong-passphrase rejection
  - simulate chunk loss and confirm verify detects it

No network calls. No credentials. No external services.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import pytest

from picard.adapters.filesystem import FilesystemAdapter
from picard.adapters.memory import MemoryAdapter
from picard.adapters.sqlite_local import SQLiteLocalAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator

_PASSPHRASE = "test-passphrase-free-adapters"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp(tmp_path):
    return tmp_path


@pytest.fixture
def orch_fs(tmp_path):
    """Single FilesystemAdapter + on-disk Index."""
    chunks_dir = tmp_path / "chunks"
    db_path = str(tmp_path / "index.db")
    adapter = FilesystemAdapter(chunks_dir)
    index = Index(db_path)
    return Orchestrator([adapter], index, block_size=256)


@pytest.fixture
def orch_sqlite(tmp_path):
    """Single SQLiteLocalAdapter (on-disk) + on-disk Index."""
    db_path = str(tmp_path / "chunks.db")
    index_path = str(tmp_path / "index.db")
    adapter = SQLiteLocalAdapter(db_path)
    index = Index(index_path)
    return Orchestrator([adapter], index, block_size=256)


@pytest.fixture
def orch_multi(tmp_path):
    """FilesystemAdapter + SQLiteLocalAdapter + MemoryAdapter — scatter across all three."""
    adapters = [
        FilesystemAdapter(tmp_path / "fs_chunks"),
        SQLiteLocalAdapter(str(tmp_path / "sq_chunks.db")),
        MemoryAdapter(name="mem", max_chunk_bytes=65536),
    ]
    index = Index(str(tmp_path / "index.db"))
    return Orchestrator(adapters, index, block_size=128)


# ---------------------------------------------------------------------------
# Parametrize the same round-trip suite over each free adapter fixture
# ---------------------------------------------------------------------------

FREE_ADAPTER_FIXTURES = ["orch_fs", "orch_sqlite"]


@pytest.mark.asyncio
@pytest.mark.parametrize("orch_fixture", FREE_ADAPTER_FIXTURES)
class TestRoundTrip:
    async def test_text_put_get(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        data = b"hello picard, this is a real round-trip"
        await orch.put("/hello.txt", data, _PASSPHRASE)
        result = await orch.get("/hello.txt", _PASSPHRASE)
        assert result == data

    async def test_binary_put_get(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        data = bytes(range(256)) * 64  # 16 KiB with every byte value
        await orch.put("/binary.bin", data, _PASSPHRASE)
        result = await orch.get("/binary.bin", _PASSPHRASE)
        assert result == data

    async def test_large_multiblock_file(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        data = b"z" * 2048  # 8 blocks at block_size=256
        manifest = await orch.put("/large.bin", data, _PASSPHRASE)
        assert manifest.total_blocks == 8
        result = await orch.get("/large.bin", _PASSPHRASE)
        assert result == data
        assert hashlib.sha256(result).hexdigest() == manifest.plaintext_hash

    async def test_empty_file(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        await orch.put("/empty.txt", b"", _PASSPHRASE)
        assert await orch.get("/empty.txt", _PASSPHRASE) == b""

    async def test_wrong_passphrase_raises(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        await orch.put("/secret.txt", b"classified", _PASSPHRASE)
        with pytest.raises(Exception):
            await orch.get("/secret.txt", "wrong-passphrase")

    async def test_verify_passes_after_put(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        await orch.put("/v.txt", b"verify me", _PASSPHRASE)
        report = await orch.verify("/v.txt")
        assert report["ok"] is True
        assert report["missing_chunks"] == 0
        assert report["corrupt_blocks"] == 0

    async def test_rm_removes_file(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        await orch.put("/rm.txt", b"delete me", _PASSPHRASE)
        assert await orch.rm("/rm.txt")
        with pytest.raises(FileNotFoundError):
            await orch.get("/rm.txt", _PASSPHRASE)

    async def test_overwrite_creates_new_version(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        await orch.put("/versioned.txt", b"v1", _PASSPHRASE)
        await orch.put("/versioned.txt", b"v2", _PASSPHRASE)
        result = await orch.get("/versioned.txt", _PASSPHRASE)
        assert result == b"v2"
        versions = await orch.list_versions("/versioned.txt")
        assert len(versions) == 2

    async def test_info_metadata(self, orch_fixture, request):
        orch = request.getfixturevalue(orch_fixture)
        data = b"metadata test"
        await orch.put("/meta.txt", data, _PASSPHRASE)
        info = await orch.info("/meta.txt")
        assert info["original_size"] == len(data)
        assert info["all_stored"] is True
        assert info["plaintext_hash"] == hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# FilesystemAdapter: persistence across orchestrator restarts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFilesystemPersistence:
    async def test_survives_orchestrator_restart(self, tmp_path):
        """Data written by one Orchestrator is readable by a fresh one."""
        chunks_dir = tmp_path / "chunks"
        db_path = str(tmp_path / "index.db")
        data = b"this must survive a restart"

        orch1 = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(db_path),
            block_size=256,
        )
        await orch1.put("/persist.txt", data, _PASSPHRASE)
        await orch1.close()

        orch2 = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(db_path),
            block_size=256,
        )
        result = await orch2.get("/persist.txt", _PASSPHRASE)
        assert result == data

    async def test_index_survives_restart(self, tmp_path):
        """ls() on the restarted orchestrator lists the previously stored path."""
        chunks_dir = tmp_path / "chunks"
        db_path = str(tmp_path / "index.db")

        orch1 = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(db_path),
            block_size=256,
        )
        await orch1.put("/a.txt", b"a", _PASSPHRASE)
        await orch1.put("/b.txt", b"b", _PASSPHRASE)
        await orch1.close()

        orch2 = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(db_path),
            block_size=256,
        )
        paths = await orch2.ls()
        assert "/a.txt" in paths
        assert "/b.txt" in paths

    async def test_chunk_files_exist_on_disk(self, tmp_path):
        """Each chunk produces a real file under the chunks directory."""
        chunks_dir = tmp_path / "chunks"
        db_path = str(tmp_path / "index.db")
        orch = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(db_path),
            block_size=256,
        )
        manifest = await orch.put("/real.txt", b"x" * 512, _PASSPHRASE)
        chunk_files = list(chunks_dir.rglob("*.pchk"))
        total_chunks = sum(len(b.chunks) for b in manifest.blocks)
        assert len(chunk_files) == total_chunks
        assert all(f.stat().st_size > 0 for f in chunk_files)


# ---------------------------------------------------------------------------
# Multi-adapter scatter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMultiAdapterScatter:
    async def test_chunks_land_on_all_three_adapters(self, orch_multi):
        # 6+ blocks at block_size=128 to guarantee round-robin hits all 3 adapters
        data = b"scatter me across backends" * 30
        manifest = await orch_multi.put("/scatter.txt", data, _PASSPHRASE)
        adapter_names = {
            cr.adapter_name
            for b in manifest.blocks
            for cr in b.chunks
        }
        assert adapter_names == {"filesystem", "sqlite_local", "mem"}

    async def test_round_trip_multi_adapter(self, orch_multi):
        data = b"multi-backend round-trip test" * 20
        await orch_multi.put("/multi.txt", data, _PASSPHRASE)
        result = await orch_multi.get("/multi.txt", _PASSPHRASE)
        assert result == data

    async def test_verify_multi_adapter(self, orch_multi):
        data = b"verify across multiple backends" * 20
        await orch_multi.put("/mv.txt", data, _PASSPHRASE)
        report = await orch_multi.verify("/mv.txt")
        assert report["ok"] is True

    async def test_rm_cleans_all_adapters(self, orch_multi):
        data = b"delete from all" * 20
        await orch_multi.put("/del.txt", data, _PASSPHRASE)
        assert await orch_multi.rm("/del.txt")
        # Verify it's gone
        with pytest.raises(FileNotFoundError):
            await orch_multi.get("/del.txt", _PASSPHRASE)


# ---------------------------------------------------------------------------
# Streaming put/get (FilesystemAdapter)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStreaming:
    async def _make_orch(self, tmp_path):
        return Orchestrator(
            [FilesystemAdapter(tmp_path / "chunks")],
            Index(str(tmp_path / "index.db")),
            block_size=256,
        )

    async def test_put_stream_get_stream_round_trip(self, tmp_path):
        orch = await self._make_orch(tmp_path)
        data = b"streaming data " * 100
        chunks = [data[i : i + 64] for i in range(0, len(data), 64)]

        async def _gen():
            for c in chunks:
                yield c

        manifest = await orch.put_stream("/stream.txt", _gen(), _PASSPHRASE)
        assert manifest.original_size == len(data)

        result = b""
        async for block in orch.get_stream("/stream.txt", _PASSPHRASE):
            result += block
        assert result == data

    async def test_put_stream_matches_put(self, tmp_path):
        orch = await self._make_orch(tmp_path)
        data = b"compare stream vs bulk put" * 50

        await orch.put("/bulk.txt", data, _PASSPHRASE)

        async def _gen():
            yield data

        await orch.put_stream("/stream.txt", _gen(), _PASSPHRASE)

        bulk_result = await orch.get("/bulk.txt", _PASSPHRASE)
        stream_result = await orch.get("/stream.txt", _PASSPHRASE)
        assert bulk_result == stream_result == data


# ---------------------------------------------------------------------------
# Directory operations (FilesystemAdapter)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDirectoryOps:
    async def test_mkdir_and_tree(self, orch_fs):
        await orch_fs.mkdir("/docs", parents=True)
        await orch_fs.put("/docs/readme.txt", b"readme", _PASSPHRASE)
        await orch_fs.put("/docs/notes.txt", b"notes", _PASSPHRASE)
        tree = await orch_fs.tree("/")
        assert any(
            n.get("path") == "/docs"
            for n in tree.get("children", [])
        )

    async def test_cp_and_mv(self, orch_fs):
        await orch_fs.put("/orig.txt", b"original", _PASSPHRASE)
        await orch_fs.cp("/orig.txt", "/copy.txt")
        assert await orch_fs.get("/copy.txt", _PASSPHRASE) == b"original"
        assert await orch_fs.get("/orig.txt", _PASSPHRASE) == b"original"

        await orch_fs.mv("/copy.txt", "/moved.txt")
        assert await orch_fs.get("/moved.txt", _PASSPHRASE) == b"original"
        with pytest.raises(FileNotFoundError):
            await orch_fs.get("/copy.txt", _PASSPHRASE)

    async def test_ls_children(self, orch_fs):
        await orch_fs.mkdir("/dir", parents=True)
        await orch_fs.put("/dir/a.txt", b"a", _PASSPHRASE)
        await orch_fs.put("/dir/b.txt", b"b", _PASSPHRASE)
        children = await orch_fs.ls("/dir")
        assert "/dir/a.txt" in children
        assert "/dir/b.txt" in children

    async def test_rm_nonempty_dir_raises(self, orch_fs):
        await orch_fs.mkdir("/full", parents=True)
        await orch_fs.put("/full/file.txt", b"x", _PASSPHRASE)
        with pytest.raises(OSError, match="not empty"):
            await orch_fs.rm("/full")


# ---------------------------------------------------------------------------
# Verify detects chunk loss (FilesystemAdapter)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestChunkLossDetection:
    async def test_verify_reports_missing_after_deletion(self, tmp_path):
        """Deleting a chunk file on disk causes verify() to report it missing."""
        chunks_dir = tmp_path / "chunks"
        orch = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(str(tmp_path / "index.db")),
            block_size=256,
        )
        await orch.put("/lossy.txt", b"data that will lose a chunk", _PASSPHRASE)

        # Manually delete one chunk file from disk
        chunk_files = list(chunks_dir.rglob("*.pchk"))
        assert chunk_files, "expected at least one .pchk file"
        chunk_files[0].unlink()

        report = await orch.verify("/lossy.txt")
        assert report["ok"] is False
        assert report["missing_chunks"] >= 1

    async def test_get_raises_after_chunk_deleted(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        orch = Orchestrator(
            [FilesystemAdapter(chunks_dir)],
            Index(str(tmp_path / "index.db")),
            block_size=256,
        )
        await orch.put("/damaged.txt", b"this will break", _PASSPHRASE)

        for f in chunks_dir.rglob("*.pchk"):
            f.unlink()

        with pytest.raises(Exception):
            await orch.get("/damaged.txt", _PASSPHRASE)
