"""Tests for the QR code adapter -- mocked, no real QR encoding/decoding."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from picard.adapters.base import StoredChunkRef
from picard.adapters.qr_code import QrCodeAdapter
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


def _make_adapter(output_dir: str | None = None, **kwargs) -> QrCodeAdapter:
    if output_dir is None:
        output_dir = tempfile.mkdtemp()
    return QrCodeAdapter(output_dir=output_dir, **kwargs)


class TestQrCodeAdapter:
    def test_profile(self):
        a = _make_adapter()
        p = a.profile()
        assert p.name == "qr-code"
        assert p.max_chunk_bytes == 2900
        assert p.persistent is True
        assert p.ordered is False

    def test_filename(self):
        assert QrCodeAdapter._filename("m1", 0, 2) == "pchk_m1_b0_c2.png"
        assert QrCodeAdapter._filename("abc", 3, 7) == "pchk_abc_b3_c7.png"

    @pytest.mark.asyncio
    async def test_write_chunk(self):
        a = _make_adapter()
        a._generate_qr = MagicMock(return_value=b"\x89PNG-fake-image-data")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)

        assert ref.adapter_name == "qr-code"
        assert ref.location == "pchk_m1_b0_c0.png"
        a._generate_qr.assert_called_once_with(enc)
        assert (Path(a._output_dir) / ref.location).exists()

    @pytest.mark.asyncio
    async def test_read_chunk(self):
        a = _make_adapter()
        enc = _sample_encoded()
        a._generate_qr = MagicMock(return_value=b"\x89PNG-fake")
        a._decode_qr = MagicMock(return_value=enc)

        ref = await a.write_chunk(enc)
        result = await a.read_chunk(ref)

        assert result == enc
        a._decode_qr.assert_called_once_with(b"\x89PNG-fake")

    @pytest.mark.asyncio
    async def test_delete_chunk(self):
        a = _make_adapter()
        a._generate_qr = MagicMock(return_value=b"\x89PNG-fake")
        enc = _sample_encoded()

        ref = await a.write_chunk(enc)
        assert (Path(a._output_dir) / ref.location).exists()

        assert await a.delete_chunk(ref) is True
        assert not (Path(a._output_dir) / ref.location).exists()

    @pytest.mark.asyncio
    async def test_delete_chunk_not_found(self):
        a = _make_adapter()
        ref = StoredChunkRef(adapter_name="qr-code", location="nonexistent.png")
        assert await a.delete_chunk(ref) is False

    @pytest.mark.asyncio
    async def test_list_chunks(self):
        a = _make_adapter()
        a._generate_qr = MagicMock(return_value=b"\x89PNG-fake")

        await a.write_chunk(_sample_encoded("m1", 0, 0))
        await a.write_chunk(_sample_encoded("m1", 0, 1))
        await a.write_chunk(_sample_encoded("m2", 0, 0))

        refs = await a.list_chunks("m1")
        assert len(refs) == 2
        assert all(r.adapter_name == "qr-code" for r in refs)
        names = {r.location for r in refs}
        assert "pchk_m1_b0_c0.png" in names
        assert "pchk_m1_b0_c1.png" in names

    @pytest.mark.asyncio
    async def test_list_chunks_empty(self):
        a = _make_adapter()
        refs = await a.list_chunks("m1")
        assert refs == []

    @pytest.mark.asyncio
    async def test_list_chunks_no_dir(self):
        a = _make_adapter(output_dir="/tmp/picard_nonexistent_dir_test")
        refs = await a.list_chunks("m1")
        assert refs == []

    @pytest.mark.asyncio
    async def test_health_check_ok(self):
        a = _make_adapter()
        assert await a.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_fail(self):
        a = _make_adapter()
        # Force health check to fail by making _ensure_dir raise
        def _bad_dir():
            raise PermissionError("denied")
        a._ensure_dir = _bad_dir
        assert await a.health_check() is False

    def test_custom_max_chunk_bytes(self):
        a = _make_adapter(max_chunk_bytes=1500)
        assert a.profile().max_chunk_bytes == 1500
