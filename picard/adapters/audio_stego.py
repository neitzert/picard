"""Audio steganography adapter — hides chunks in WAV sample LSBs.

Each chunk is encoded into the least-significant bits of 16-bit PCM
audio samples.  The resulting WAV file sounds like faint static —
indistinguishable from noise floor in a real recording.

Uses only the Python stdlib (wave, struct).
"""

from __future__ import annotations

import os
import struct
import uuid
import wave
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk


class AudioStegoAdapter(Adapter):
    """Scatter chunks into WAV audio files via LSB steganography."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        sample_rate: int = 44100,
        channels: int = 1,
        max_chunk_bytes: int = 65536,
    ) -> None:
        self._dir = Path(output_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._rate = sample_rate
        self._channels = channels
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="audio-stego",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=True,
            rate_limit_per_min=60,
        )

    def _encode_lsb(self, data: bytes) -> list[int]:
        length_bits = format(len(data), "032b")
        data_bits = "".join(format(b, "08b") for b in data)
        all_bits = length_bits + data_bits

        samples = []
        for bit in all_bits:
            base = 0
            sample = (base & 0xFFFE) | int(bit)
            samples.append(sample)

        padding = max(0, self._rate - len(samples))
        for _ in range(padding):
            samples.append(0)

        return samples

    def _decode_lsb(self, samples: list[int]) -> bytes:
        if len(samples) < 32:
            raise ValueError("not enough samples")

        length_bits = ""
        for s in samples[:32]:
            length_bits += str(s & 1)
        length = int(length_bits, 2)

        total_bits_needed = 32 + length * 8
        if len(samples) < total_bits_needed:
            raise ValueError("not enough samples for declared length")

        data_bits = ""
        for s in samples[32:total_bits_needed]:
            data_bits += str(s & 1)

        result = bytearray()
        for i in range(0, len(data_bits), 8):
            result.append(int(data_bits[i : i + 8], 2))

        return bytes(result)

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            file_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            file_id = uuid.uuid4().hex

        raw = encoded_chunk.encode("utf-8")
        samples = self._encode_lsb(raw)

        path = self._dir / f"{file_id}.wav"

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self._channels)
            wf.setsampwidth(2)
            wf.setframerate(self._rate)
            frame_data = b"".join(struct.pack("<h", s) for s in samples)
            wf.writeframes(frame_data)

        loc = f"audio://{path}"
        return StoredChunkRef(adapter_name="audio-stego", location=loc)

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = ref.location.removeprefix("audio://")

        with wave.open(path, "rb") as wf:
            n_frames = wf.getnframes()
            raw_frames = wf.readframes(n_frames)

        samples = []
        for i in range(0, len(raw_frames), 2):
            samples.append(struct.unpack("<h", raw_frames[i : i + 2])[0])

        data = self._decode_lsb(samples)
        return data.decode("utf-8")

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = Path(ref.location.removeprefix("audio://"))
        if path.exists():
            path.unlink()
            return True
        return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs = []
        for f in self._dir.glob("*.wav"):
            if manifest_id in f.stem:
                refs.append(
                    StoredChunkRef(
                        adapter_name="audio-stego", location=f"audio://{f}"
                    )
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() and os.access(self._dir, os.W_OK)

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        usage = shutil.disk_usage(self._dir)
        files = list(self._dir.glob("*.wav"))
        used = sum(f.stat().st_size for f in files)
        return {
            "used_bytes": used,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "chunk_count": len(files),
        }
