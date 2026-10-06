"""Tests for the Homebrew tap adapter -- mocked, no real git or tap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.homebrew_adapter import HomebrewAdapter
from picard.chunker import Chunk


def _sample_encoded(manifest_id: str = "m1", block: int = 0, idx: int = 0) -> str:
    return Chunk(
        version=1, manifest_id=manifest_id, block_index=block,
        chunk_index=idx, total_chunks=1, payload=b"test-payload",
    ).encode()


@pytest.mark.asyncio
class TestHomebrewProfile:
    async def test_defaults(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        p = a.profile()
        assert p.name == "homebrew"
        assert p.max_chunk_bytes == 8192
        assert p.persistent is True

    async def test_custom_max(self, tmp_path):
        a = HomebrewAdapter(tmp_path, max_chunk_bytes=1000)
        assert a.profile().max_chunk_bytes == 1000


@pytest.mark.asyncio
class TestHomebrewWrite:
    async def test_write_chunk(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        enc = _sample_encoded()

        with patch.object(a, "_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            ref = await a.write_chunk(enc)

        assert ref.adapter_name == "homebrew"
        assert ref.location.startswith("brew://")
        formula_dir = tmp_path / "Formula"
        assert formula_dir.exists()
        rb_files = list(formula_dir.glob("pchk-*.rb"))
        assert len(rb_files) == 1

    async def test_write_opaque(self, tmp_path):
        a = HomebrewAdapter(tmp_path)

        with patch.object(a, "_git", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = ""
            ref = await a.write_chunk("opaque-sealed-data")

        assert ref.adapter_name == "homebrew"

    async def test_formula_contains_data(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        enc = _sample_encoded()

        with patch.object(a, "_git", new_callable=AsyncMock):
            await a.write_chunk(enc)

        formula_dir = tmp_path / "Formula"
        rb = list(formula_dir.glob("pchk-*.rb"))[0]
        content = rb.read_text()
        assert "desc" in content


@pytest.mark.asyncio
class TestHomebrewRead:
    async def test_read_chunk(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        enc = _sample_encoded()

        with patch.object(a, "_git", new_callable=AsyncMock):
            ref = await a.write_chunk(enc)

        result = await a.read_chunk(ref)
        assert result == enc

    async def test_read_not_found(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        ref = StoredChunkRef(adapter_name="homebrew", location=f"brew://{tmp_path.name}/pchk-missing")
        with pytest.raises(KeyError):
            await a.read_chunk(ref)


@pytest.mark.asyncio
class TestHomebrewDelete:
    async def test_delete_success(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        enc = _sample_encoded()

        with patch.object(a, "_git", new_callable=AsyncMock):
            ref = await a.write_chunk(enc)
            result = await a.delete_chunk(ref)

        assert result is True

    async def test_delete_not_found(self, tmp_path):
        a = HomebrewAdapter(tmp_path)
        ref = StoredChunkRef(adapter_name="homebrew", location=f"brew://{tmp_path.name}/pchk-missing")
        assert await a.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestHomebrewList:
    async def test_list_chunks(self, tmp_path):
        a = HomebrewAdapter(tmp_path)

        with patch.object(a, "_git", new_callable=AsyncMock):
            await a.write_chunk(_sample_encoded("m1", 0, 0))
            await a.write_chunk(_sample_encoded("m1", 0, 1))

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "homebrew" for r in refs)


@pytest.mark.asyncio
class TestHomebrewHealth:
    async def test_health_ok(self, tmp_path):
        (tmp_path / ".git").mkdir()
        a = HomebrewAdapter(tmp_path)
        assert await a.health_check() is True

    async def test_health_fail(self, tmp_path):
        a = HomebrewAdapter(tmp_path / "nonexistent")
        assert await a.health_check() is False
