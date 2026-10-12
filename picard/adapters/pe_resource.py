"""PE resource adapter — stores chunks in Windows PE binary custom resources.

Each chunk becomes a minimal PE file with data in a custom resource:
    pe://{output_dir}/{chunk_id}.exe

Pure-Python — produces a valid Windows PE binary container.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_MZ_STUB = b"MZ" + b"\x00" * 58 + struct.pack("<I", 64)
_PE_SIG = b"PE\x00\x00"


class PeResourceAdapter(Adapter):
    """Store chunks in Windows PE binary custom resources."""

    def __init__(self, output_dir: str | Path, *, max_chunk_bytes: int = 1_048_576) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="pe-resource",
            max_chunk_bytes=self._max,
            persistent=True,
            ordered=False,
            rate_limit_per_min=120,
        )

    async def write_chunk(self, encoded_chunk: str) -> StoredChunkRef:
        try:
            chunk = Chunk.decode(encoded_chunk)
            chunk_id = f"{chunk.manifest_id}_{chunk.block_index}_{chunk.chunk_index}"
        except ValueError:
            chunk_id = uuid.uuid4().hex

        path = self._dir / f"{chunk_id}.exe"

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            data = encoded_chunk.encode("utf-8")
            marker = b"PICARD_DATA_START"
            end_marker = b"PICARD_DATA_END"

            section_alignment = 0x1000
            file_alignment = 0x200

            pe_hdr_size = len(_MZ_STUB) + len(_PE_SIG)
            coff_size = 20
            optional_size = 240
            section_hdr_size = 40
            num_sections = 1
            headers_raw = pe_hdr_size + coff_size + optional_size + section_hdr_size * num_sections
            headers_aligned = (headers_raw + file_alignment - 1) & ~(file_alignment - 1)

            section_data = marker + struct.pack("<I", len(data)) + data + end_marker
            section_raw_size = (len(section_data) + file_alignment - 1) & ~(file_alignment - 1)
            section_virtual_size = len(section_data)

            coff = struct.pack(
                "<HHIIIHH",
                0x8664,  # Machine: AMD64
                num_sections,
                0,  # TimeDateStamp
                0, 0,  # PointerToSymbolTable, NumberOfSymbols
                optional_size,
                0x0022,  # Characteristics: EXECUTABLE_IMAGE | LARGE_ADDRESS_AWARE
            )

            optional = struct.pack("<H", 0x020B)  # PE32+
            optional += b"\x00" * (optional_size - 2)

            section_name = b".picard\x00"
            section_hdr = struct.pack(
                "<8sIIIIIIHHI",
                section_name,
                section_virtual_size,
                section_alignment,
                section_raw_size,
                headers_aligned,
                0, 0, 0, 0,
                0x40000040,  # INITIALIZED_DATA | READ
            )

            with open(path, "wb") as f:
                f.write(_MZ_STUB)
                f.write(_PE_SIG)
                f.write(coff)
                f.write(optional)
                f.write(section_hdr)
                f.write(b"\x00" * (headers_aligned - f.tell()))
                f.write(section_data)
                f.write(b"\x00" * (section_raw_size - len(section_data)))

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="pe-resource", location=f"pe://{path}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = Path(ref.location.split("://", 1)[1])

        def _read():
            with open(path, "rb") as f:
                raw = f.read()

            marker = b"PICARD_DATA_START"
            idx = raw.find(marker)
            if idx < 0:
                raise KeyError(f"PICARD marker not found in {path}")
            idx += len(marker)
            data_len = struct.unpack_from("<I", raw, idx)[0]
            idx += 4
            return raw[idx:idx + data_len].decode("utf-8")

        return await asyncio.to_thread(_read)

    async def delete_chunk(self, ref: StoredChunkRef) -> bool:
        path = Path(ref.location.split("://", 1)[1])
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def list_chunks(self, manifest_id: str) -> Sequence[StoredChunkRef]:
        refs: list[StoredChunkRef] = []
        if self._dir.is_dir():
            for p in self._dir.glob(f"{manifest_id}_*.exe"):
                refs.append(
                    StoredChunkRef(adapter_name="pe-resource", location=f"pe://{p}")
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() or not self._dir.exists()

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        if not self._dir.exists():
            return {"used_bytes": 0, "free_bytes": None, "total_bytes": None, "chunk_count": 0}
        usage = shutil.disk_usage(self._dir)
        count = sum(1 for _ in self._dir.glob("*.exe"))
        used = sum(f.stat().st_size for f in self._dir.glob("*.exe"))
        return {"used_bytes": used, "free_bytes": usage.free, "total_bytes": usage.total, "chunk_count": count}
