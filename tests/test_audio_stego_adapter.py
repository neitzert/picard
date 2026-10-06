"""Tests for audio steganography adapter (WAV LSB encoding)."""

import struct
import tempfile
import wave
from pathlib import Path

import pytest

from picard.adapters.audio_stego import AudioStegoAdapter


@pytest.fixture
def adapter(tmp_path):
    return AudioStegoAdapter(tmp_path)


class TestProfile:
    def test_name(self, adapter):
        assert adapter.profile().name == "audio-stego"

    def test_persistent(self, adapter):
        assert adapter.profile().persistent is True

    def test_max_chunk_bytes(self, adapter):
        assert adapter.profile().max_chunk_bytes == 65536

    def test_rate_limit(self, adapter):
        assert adapter.profile().rate_limit_per_min == 60


@pytest.mark.asyncio
class TestWriteRead:
    async def test_round_trip(self, adapter):
        data = "PCHK:m1:0:0:hello world"
        ref = await adapter.write_chunk(data)

        assert ref.adapter_name == "audio-stego"
        assert ref.location.startswith("audio://")
        assert ref.location.endswith(".wav")

        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_round_trip_longer_data(self, adapter):
        data = "PCHK:m1:0:0:" + "x" * 200
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data

    async def test_wav_file_created(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        path = Path(ref.location.removeprefix("audio://"))
        assert path.exists()
        assert path.suffix == ".wav"

    async def test_wav_is_valid(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:test")
        path = ref.location.removeprefix("audio://")

        with wave.open(path, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 44100
            assert wf.getnframes() > 0

    async def test_opaque_data_round_trip(self, adapter):
        data = "opaque-sealed-no-pchk-header"
        ref = await adapter.write_chunk(data)
        result = await adapter.read_chunk(ref)
        assert result == data


@pytest.mark.asyncio
class TestDelete:
    async def test_delete_existing(self, adapter):
        ref = await adapter.write_chunk("PCHK:m1:0:0:data")
        path = Path(ref.location.removeprefix("audio://"))
        assert path.exists()

        result = await adapter.delete_chunk(ref)
        assert result is True
        assert not path.exists()

    async def test_delete_nonexistent(self, adapter):
        from picard.adapters.base import StoredChunkRef

        ref = StoredChunkRef(
            adapter_name="audio-stego",
            location="audio:///nonexistent/file.wav",
        )
        assert await adapter.delete_chunk(ref) is False


@pytest.mark.asyncio
class TestListChunks:
    async def test_list_by_manifest(self, adapter, tmp_path):
        (tmp_path / "manifest1_0_0.wav").write_bytes(b"\x00")
        (tmp_path / "manifest1_0_1.wav").write_bytes(b"\x00")
        (tmp_path / "other_0_0.wav").write_bytes(b"\x00")

        refs = await adapter.list_chunks("manifest1")
        assert len(refs) == 2


@pytest.mark.asyncio
class TestHealth:
    async def test_healthy(self, adapter):
        assert await adapter.health_check() is True

    async def test_unhealthy(self, tmp_path):
        bad = AudioStegoAdapter(tmp_path / "sub")
        bad._dir = Path(tmp_path / "does_not_exist_at_all")
        assert await bad.health_check() is False


@pytest.mark.asyncio
class TestCapacity:
    async def test_empty(self, adapter):
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 0
        assert cap["used_bytes"] == 0
        assert cap["free_bytes"] is not None

    async def test_after_write(self, adapter):
        await adapter.write_chunk("PCHK:m1:0:0:data")
        cap = await adapter.capacity()
        assert cap["chunk_count"] == 1
        assert cap["used_bytes"] > 0
