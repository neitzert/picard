"""ELF section adapter — stores chunks in custom ELF binary sections.

Each chunk becomes an ELF binary with a .picard section containing the data:
    elf://{output_dir}/{chunk_id}.elf

Pure-Python — no external deps. Hides data in legitimate-looking binaries.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

_ELF_MAGIC = b"\x7fELF"
_SECTION_NAME = b".picard\x00"


class ElfSectionAdapter(Adapter):
    """Hide chunks in custom ELF binary sections."""

    def __init__(self, output_dir: str | Path, *, max_chunk_bytes: int = 1_048_576) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="elf-section",
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

        path = self._dir / f"{chunk_id}.elf"

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            data = encoded_chunk.encode("utf-8")

            shstrtab = b"\x00.shstrtab\x00.picard\x00"
            shstrtab_name_offset = 1
            picard_name_offset = 11

            ehdr_size = 64
            shdr_size = 64
            num_sections = 3

            data_offset = ehdr_size
            shstrtab_offset = data_offset + len(data)
            shdr_offset = shstrtab_offset + len(shstrtab)
            # Align to 8
            shdr_offset = (shdr_offset + 7) & ~7
            padding = shdr_offset - (shstrtab_offset + len(shstrtab))

            ehdr = struct.pack(
                "<4sBBBBB7sHHIQQQIHHHHHH",
                _ELF_MAGIC,
                2, 1, 1, 0, 0,  # 64-bit, little-endian, ELFv1, SYSV, pad
                b"\x00" * 7,
                2,  # ET_EXEC
                62,  # EM_X86_64
                1,
                0, 0,
                shdr_offset,
                0,
                ehdr_size,
                0, 0,
                shdr_size,
                num_sections,
                1,  # shstrndx
            )

            # Null section header
            null_shdr = struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

            # .shstrtab section header
            shstrtab_shdr = struct.pack(
                "<IIQQQQIIQQ",
                shstrtab_name_offset,
                3,  # SHT_STRTAB
                0, 0,
                shstrtab_offset,
                len(shstrtab),
                0, 0, 1, 0,
            )

            # .picard section header
            picard_shdr = struct.pack(
                "<IIQQQQIIQQ",
                picard_name_offset,
                1,  # SHT_PROGBITS
                0, 0,
                data_offset,
                len(data),
                0, 0, 1, 0,
            )

            with open(path, "wb") as f:
                f.write(ehdr)
                f.write(data)
                f.write(shstrtab)
                f.write(b"\x00" * padding)
                f.write(null_shdr)
                f.write(shstrtab_shdr)
                f.write(picard_shdr)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="elf-section", location=f"elf://{path}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = Path(ref.location.split("://", 1)[1])

        def _read():
            with open(path, "rb") as f:
                raw = f.read()

            if raw[:4] != _ELF_MAGIC:
                raise ValueError(f"Not an ELF file: {path}")

            e_shoff = struct.unpack_from("<Q", raw, 40)[0]
            e_shentsize = struct.unpack_from("<H", raw, 58)[0]
            e_shnum = struct.unpack_from("<H", raw, 60)[0]
            e_shstrndx = struct.unpack_from("<H", raw, 62)[0]

            strtab_off = struct.unpack_from("<Q", raw, e_shoff + e_shstrndx * e_shentsize + 24)[0]
            strtab_sz = struct.unpack_from("<Q", raw, e_shoff + e_shstrndx * e_shentsize + 32)[0]
            strtab = raw[strtab_off:strtab_off + strtab_sz]

            for i in range(e_shnum):
                base = e_shoff + i * e_shentsize
                name_idx = struct.unpack_from("<I", raw, base)[0]
                name_end = strtab.index(b"\x00", name_idx)
                name = strtab[name_idx:name_end]
                if name == b".picard":
                    sec_off = struct.unpack_from("<Q", raw, base + 24)[0]
                    sec_sz = struct.unpack_from("<Q", raw, base + 32)[0]
                    return raw[sec_off:sec_off + sec_sz].decode("utf-8")

            raise KeyError(f".picard section not found in {path}")

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
            for p in self._dir.glob(f"{manifest_id}_*.elf"):
                refs.append(
                    StoredChunkRef(adapter_name="elf-section", location=f"elf://{p}")
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() or not self._dir.exists()

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        if not self._dir.exists():
            return {"used_bytes": 0, "free_bytes": None, "total_bytes": None, "chunk_count": 0}
        usage = shutil.disk_usage(self._dir)
        count = sum(1 for _ in self._dir.glob("*.elf"))
        used = sum(f.stat().st_size for f in self._dir.glob("*.elf"))
        return {"used_bytes": used, "free_bytes": usage.free, "total_bytes": usage.total, "chunk_count": count}
