"""Orchestrator — the flock's shared instinct.

Coordinates encryption, chunking, scattering across adapters,
and the reverse path: gathering, verifying, reassembling, decrypting.
"""

from __future__ import annotations

import copy
import hashlib
import math
import uuid
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
            plaintext_hash=hashlib.sha256(data).hexdigest(),
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
                chunk_digest = hashlib.sha256(
                    wire_str.encode() if isinstance(wire_str, str) else wire_str
                ).hexdigest()
                block.chunks.append(
                    ChunkRecord(
                        chunk_index=chunk.chunk_index,
                        adapter_name=ref.adapter_name,
                        location=ref.location,
                        status=ChunkStatus.STORED,
                        size_bytes=len(wire_str),
                        digest=chunk_digest,
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

    async def info(self, path: str) -> dict:
        """Return detailed metadata about a stored file."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")

        adapter_distribution: dict[str, int] = {}
        total_chunk_bytes = 0
        for block in manifest.blocks:
            for cr in block.chunks:
                adapter_distribution[cr.adapter_name] = (
                    adapter_distribution.get(cr.adapter_name, 0) + 1
                )
                total_chunk_bytes += cr.size_bytes

        return {
            "path": manifest.path,
            "manifest_id": manifest.manifest_id,
            "original_size": manifest.original_size,
            "block_size": manifest.block_size,
            "total_blocks": manifest.total_blocks,
            "total_chunks": manifest.total_chunks,
            "total_chunk_bytes": total_chunk_bytes,
            "created_at": manifest.created_at,
            "plaintext_hash": manifest.plaintext_hash,
            "adapter_distribution": adapter_distribution,
            "tags": manifest.tags,
            "all_stored": manifest.all_stored,
        }

    async def verify(self, path: str) -> dict:
        """Verify integrity of all chunks for a file."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")

        report: dict = {
            "path": manifest.path,
            "manifest_id": manifest.manifest_id,
            "total_blocks": manifest.total_blocks,
            "total_chunks": manifest.total_chunks,
            "verified_chunks": 0,
            "missing_chunks": 0,
            "corrupt_blocks": 0,
            "errors": [],
        }

        for block in sorted(manifest.blocks, key=lambda b: b.block_index):
            raw_chunks: list[Chunk] = []
            block_ok = True
            for cr in block.chunks:
                try:
                    adapter = self._adapter_by_name(cr.adapter_name)
                    ref = StoredChunkRef(
                        adapter_name=cr.adapter_name, location=cr.location
                    )
                    wire_str = await adapter.read_chunk(ref)
                    if self._key_ring:
                        sub_key = self._key_ring.get_sub_key(cr.adapter_name)
                        encoded = unseal(bytes.fromhex(wire_str), sub_key)
                    else:
                        encoded = wire_str
                    raw_chunks.append(Chunk.decode(encoded))
                    report["verified_chunks"] += 1
                except Exception as e:
                    report["missing_chunks"] += 1
                    report["errors"].append(
                        {
                            "block_index": block.block_index,
                            "chunk_index": cr.chunk_index,
                            "adapter": cr.adapter_name,
                            "error": str(e),
                        }
                    )
                    block_ok = False

            if block_ok and raw_chunks:
                ct = reassemble(raw_chunks)
                if block_digest(ct) != block.digest:
                    report["corrupt_blocks"] += 1
                    report["errors"].append(
                        {
                            "block_index": block.block_index,
                            "error": "block digest mismatch",
                        }
                    )

        report["ok"] = (
            report["missing_chunks"] == 0 and report["corrupt_blocks"] == 0
        )
        return report

    async def cp(self, src_path: str, dst_path: str) -> bool:
        """Copy a virtual file to a new path (re-indexes, no re-scatter)."""
        manifest = self._index.get_manifest_by_path(src_path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {src_path}")
        new_manifest = copy.deepcopy(manifest)
        new_manifest.manifest_id = uuid.uuid4().hex
        new_manifest.path = dst_path
        new_manifest.created_at = datetime.now(timezone.utc).isoformat()
        self._index.save_manifest(new_manifest)
        return True

    async def mv(self, src_path: str, dst_path: str) -> bool:
        """Rename a virtual file path."""
        manifest = self._index.get_manifest_by_path(src_path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {src_path}")
        manifest.path = dst_path
        self._index.save_manifest(manifest)
        return True

    async def du(self) -> dict:
        """Storage usage summary per adapter."""
        paths = self._index.list_paths()
        adapter_stats: dict[str, dict] = {}
        total_bytes = 0
        total_chunks = 0

        for path in paths:
            manifest = self._index.get_manifest_by_path(path)
            if manifest is None:
                continue
            for block in manifest.blocks:
                for cr in block.chunks:
                    if cr.adapter_name not in adapter_stats:
                        adapter_stats[cr.adapter_name] = {
                            "chunk_count": 0,
                            "bytes": 0,
                        }
                    adapter_stats[cr.adapter_name]["chunk_count"] += 1
                    adapter_stats[cr.adapter_name]["bytes"] += cr.size_bytes
                    total_bytes += cr.size_bytes
                    total_chunks += 1

        for adapter in self._adapters:
            name = adapter.profile().name
            if name in adapter_stats:
                try:
                    cap = await adapter.capacity()
                    adapter_stats[name]["capacity"] = cap
                except Exception:
                    pass

        return {
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "total_files": len(paths),
            "adapters": adapter_stats,
        }

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
