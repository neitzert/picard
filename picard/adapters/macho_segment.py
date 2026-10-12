"""Mach-O segment adapter — stores chunks in custom Mach-O binary segments.

Each chunk becomes a minimal Mach-O with data in a __PICARD segment:
    macho://{output_dir}/{chunk_id}.macho

Pure-Python — produces a valid 64-bit Mach-O binary container.
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from pathlib import Path
from typing import Sequence

from picard.adapters.base import Adapter, AdapterProfile, StoredChunkRef
from picard.chunker import Chunk

MH_MAGIC_64 = 0xFEEDFACF
MH_EXECUTE = 0x2
CPU_TYPE_X86_64 = 0x01000007
CPU_SUBTYPE_ALL = 0x3
LC_SEGMENT_64 = 0x19


class MachoSegmentAdapter(Adapter):
    """Store chunks in custom Mach-O binary segments."""

    def __init__(self, output_dir: str | Path, *, max_chunk_bytes: int = 1_048_576) -> None:
        self._dir = Path(output_dir)
        self._max = max_chunk_bytes

    def profile(self) -> AdapterProfile:
        return AdapterProfile(
            name="macho-segment",
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

        path = self._dir / f"{chunk_id}.macho"

        def _write():
            self._dir.mkdir(parents=True, exist_ok=True)
            data = encoded_chunk.encode("utf-8")

            # Mach-O header: 32 bytes
            header_size = 32
            # LC_SEGMENT_64 command: 72 bytes (no sections)
            seg_cmd_size = 72
            total_header = header_size + seg_cmd_size

            # Page-align data offset
            page_size = 4096
            data_offset = (total_header + page_size - 1) & ~(page_size - 1)
            data_size = len(data)
            file_size = data_offset + data_size

            mh = struct.pack(
                "<IIIIIIII",
                MH_MAGIC_64,
                CPU_TYPE_X86_64,
                CPU_SUBTYPE_ALL,
                MH_EXECUTE,
                1,  # ncmds
                seg_cmd_size,  # sizeofcmds
                0,  # flags
                0,  # reserved
            )

            segname = b"__PICARD\x00\x00\x00\x00\x00\x00\x00\x00"  # 16 bytes
            seg_cmd = struct.pack(
                "<II16sQQQQIIII",
                LC_SEGMENT_64,
                seg_cmd_size,
                segname,
                0,  # vmaddr
                data_size,  # vmsize
                data_offset,  # fileoff
                data_size,  # filesize
                0, 0,  # maxprot, initprot
                0,  # nsects
                0,  # flags
            )

            with open(path, "wb") as f:
                f.write(mh)
                f.write(seg_cmd)
                f.write(b"\x00" * (data_offset - total_header))
                f.write(data)

        await asyncio.to_thread(_write)
        return StoredChunkRef(adapter_name="macho-segment", location=f"macho://{path}")

    async def read_chunk(self, ref: StoredChunkRef) -> str:
        path = Path(ref.location.split("://", 1)[1])

        def _read():
            with open(path, "rb") as f:
                raw = f.read()

            magic = struct.unpack_from("<I", raw, 0)[0]
            if magic != MH_MAGIC_64:
                raise ValueError(f"Not a 64-bit Mach-O: {path}")

            ncmds = struct.unpack_from("<I", raw, 16)[0]
            offset = 32  # past header

            for _ in range(ncmds):
                cmd, cmdsize = struct.unpack_from("<II", raw, offset)
                if cmd == LC_SEGMENT_64:
                    segname = raw[offset + 8:offset + 24].split(b"\x00")[0]
                    if segname == b"__PICARD":
                        fileoff = struct.unpack_from("<Q", raw, offset + 40)[0]
                        filesize = struct.unpack_from("<Q", raw, offset + 48)[0]
                        return raw[fileoff:fileoff + filesize].decode("utf-8")
                offset += cmdsize

            raise KeyError(f"__PICARD segment not found in {path}")

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
            for p in self._dir.glob(f"{manifest_id}_*.macho"):
                refs.append(
                    StoredChunkRef(adapter_name="macho-segment", location=f"macho://{p}")
                )
        return refs

    async def health_check(self) -> bool:
        return self._dir.is_dir() or not self._dir.exists()

    async def capacity(self) -> dict[str, int | None]:
        import shutil

        if not self._dir.exists():
            return {"used_bytes": 0, "free_bytes": None, "total_bytes": None, "chunk_count": 0}
        usage = shutil.disk_usage(self._dir)
        count = sum(1 for _ in self._dir.glob("*.macho"))
        used = sum(f.stat().st_size for f in self._dir.glob("*.macho"))
        return {"used_bytes": used, "free_bytes": usage.free, "total_bytes": usage.total, "chunk_count": count}
