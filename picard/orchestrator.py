"""Orchestrator — the flock's shared instinct.

Coordinates encryption, chunking, scattering across adapters,
and the reverse path: gathering, verifying, reassembling, decrypting.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence

from picard.adapters.base import Adapter, StoredChunkRef
from picard.chunker import Chunk, chunkify, reassemble
from picard.crypto import decrypt, encrypt
from picard.envelope import seal, unseal
from picard.index import Index
from picard.keys import KeyRing
from picard.manifest import (
    BlockRecord,
    ChunkRecord,
    ChunkStatus,
    Manifest,
    block_digest,
)

# Default block size — how much plaintext goes into one encrypted block.
DEFAULT_BLOCK_SIZE = 64 * 1024  # 64 KiB


class Orchestrator:
    """High-level API: put / get / ls / rm over an adapter set + index."""

    def __init__(
        self,
        adapters: Sequence[Adapter],
        index: Index,
        *,
        block_size: int = DEFAULT_BLOCK_SIZE,
        key_ring: KeyRing | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("need at least one adapter")
        self._adapters = list(adapters)
        self._index = index
        self._block_size = block_size
        self._key_ring = key_ring

    # --- public API --------------------------------------------------------

    async def put(self, path: str, data: bytes, passphrase: str) -> Manifest:
        """Encrypt, chunk, scatter *data* and return its manifest."""
        total_blocks = max(1, math.ceil(len(data) / self._block_size))
        manifest = Manifest(
            path=path,
            original_size=len(data),
            block_size=self._block_size,
            total_blocks=total_blocks,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        for bi in range(total_blocks):
            start = bi * self._block_size
            end = start + self._block_size
            plaintext_block = data[start:end]

            ct = encrypt(plaintext_block, passphrase)
            digest = block_digest(ct)

            block = BlockRecord(
                block_index=bi,
                digest=digest,
                size_bytes=len(ct),
            )

            # Pick the adapter with the most capacity for this block
            adapter = self._pick_adapter()
            prof = adapter.profile()

            chunks = chunkify(ct, manifest.manifest_id, bi, prof.max_chunk_bytes)

            for chunk in chunks:
                encoded = chunk.encode()
                if self._key_ring:
                    sub_key = self._key_ring.get_sub_key(prof.name)
                    wire = seal(encoded, sub_key, manifest.manifest_id)
                    wire_str = wire.hex()
                else:
                    wire_str = encoded
                ref = await adapter.write_chunk(wire_str)
                block.chunks.append(
                    ChunkRecord(
                        chunk_index=chunk.chunk_index,
                        adapter_name=ref.adapter_name,
                        location=ref.location,
                        status=ChunkStatus.STORED,
                        size_bytes=len(wire_str),
                    )
                )

            manifest.blocks.append(block)

        self._index.save_manifest(manifest)
        return manifest

    async def get(self, path: str, passphrase: str) -> bytes:
        """Retrieve and decrypt the file at *path*."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")

        plaintext_blocks: list[bytes] = []

        for block in sorted(manifest.blocks, key=lambda b: b.block_index):
            raw_chunks: list[Chunk] = []
            for cr in block.chunks:
                adapter = self._adapter_by_name(cr.adapter_name)
                ref = StoredChunkRef(adapter_name=cr.adapter_name, location=cr.location)
                wire_str = await adapter.read_chunk(ref)
                if self._key_ring:
                    sub_key = self._key_ring.get_sub_key(cr.adapter_name)
                    encoded = unseal(bytes.fromhex(wire_str), sub_key)
                else:
                    encoded = wire_str
                raw_chunks.append(Chunk.decode(encoded))

            ct = reassemble(raw_chunks)

            # Verify digest
            if block_digest(ct) != block.digest:
                raise ValueError(
                    f"block {block.block_index} digest mismatch — data may be corrupted"
                )

            plaintext_blocks.append(decrypt(ct, passphrase))

        return b"".join(plaintext_blocks)

    async def ls(self) -> list[str]:
        """List all stored virtual paths."""
        return self._index.list_paths()

    async def rm(self, path: str) -> bool:
        """Delete a file's chunks from adapters and its manifest from the index."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            return False

        for block in manifest.blocks:
            for cr in block.chunks:
                adapter = self._adapter_by_name(cr.adapter_name)
                ref = StoredChunkRef(adapter_name=cr.adapter_name, location=cr.location)
                await adapter.delete_chunk(ref)

        self._index.delete_manifest(manifest.manifest_id)
        return True

    # --- internal ----------------------------------------------------------

    def _pick_adapter(self) -> Adapter:
        """Simple strategy: use the first healthy adapter.

        Future: round-robin, capacity-aware, multi-adapter scatter.
        """
        return self._adapters[0]

    def _adapter_by_name(self, name: str) -> Adapter:
        for a in self._adapters:
            if a.profile().name == name:
                return a
        raise LookupError(f"no adapter named {name!r}")
