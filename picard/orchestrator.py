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
from collections.abc import AsyncIterator
from typing import Sequence

from picard.adapters.base import Adapter, StoredChunkRef
from picard.cache import BlockCache
from picard.chunker import Chunk, chunkify, reassemble
from picard.crypto import decrypt, encrypt
from picard.envelope import seal, unseal
from picard.index import Index
from picard.journal import Journal
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
        block_cache: BlockCache | None = None,
        journal: Journal | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("need at least one adapter")
        self._adapters = list(adapters)
        self._index = index
        self._block_size = block_size
        self._key_ring = key_ring
        self._block_cache = block_cache
        self._journal = journal
        self._next_adapter: int = 0

    async def close(self) -> None:
        for adapter in self._adapters:
            await adapter.close()

    # --- public API --------------------------------------------------------

    async def put(
        self,
        path: str,
        data: bytes,
        passphrase: str,
        *,
        mode: int = 0o100644,
    ) -> Manifest:
        """Encrypt, chunk, scatter *data* and return its manifest."""
        total_blocks = math.ceil(len(data) / self._block_size) if data else 0
        now = datetime.now(timezone.utc).isoformat()
        parent = self._parent_of(path)
        version = self._index.get_next_version(path)
        manifest = Manifest(
            path=path,
            original_size=len(data),
            block_size=self._block_size,
            total_blocks=total_blocks,
            created_at=now,
            plaintext_hash=hashlib.sha256(data).hexdigest(),
            mtime=now,
            ctime=now,
            atime=now,
            mode=mode,
            version=version,
            parent_path=parent,
        )

        jid = None
        if self._journal:
            jid = self._journal.begin("put", path, manifest.manifest_id)
        try:
            for bi in range(total_blocks):
                start = bi * self._block_size
                end = start + self._block_size
                plaintext_block = data[start:end]
                await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                if self._journal and jid:
                    self._journal.mark_in_progress(
                        jid, {"blocks_written": list(range(bi + 1))}
                    )

            if self._journal and jid:
                with self._index.connection:
                    self._index._save_manifest_raw(manifest)
                    self._journal._commit_raw(jid)
            else:
                self._index.save_manifest(manifest)
        except Exception:
            if self._journal and jid:
                self._journal.rollback(jid)
            raise
        return manifest

    async def get(
        self, path: str, passphrase: str, *, follow_symlinks: bool = True,
    ) -> bytes:
        """Retrieve and decrypt the file at *path*."""
        manifest = self._resolve_path(path, follow_symlinks)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        if manifest.file_type != "regular":
            raise FileNotFoundError(f"not a regular file: {path}")
        parts: list[bytes] = []
        for block in sorted(manifest.blocks, key=lambda b: b.block_index):
            parts.append(await self._decrypt_block(manifest, block, passphrase))
        return b"".join(parts)

    async def get_block(
        self, path: str, block_index: int, passphrase: str,
    ) -> bytes:
        """Fetch, reassemble, and decrypt a single block of *path*."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        for block in manifest.blocks:
            if block.block_index == block_index:
                return await self._decrypt_block(manifest, block, passphrase)
        raise IndexError(
            f"block {block_index} not found (file has {manifest.total_blocks} blocks)"
        )

    async def read_range(
        self, path: str, offset: int, length: int, passphrase: str,
    ) -> bytes:
        """Read *length* bytes starting at *offset* within *path*."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        if offset >= manifest.original_size:
            return b""
        end = min(offset + length, manifest.original_size)
        bs = manifest.block_size
        start_bi = offset // bs
        end_bi = (end - 1) // bs

        blocks_by_idx = {b.block_index: b for b in manifest.blocks}
        result: list[bytes] = []
        for bi in range(start_bi, end_bi + 1):
            block = blocks_by_idx.get(bi)
            if block is None:
                raise ValueError(f"missing block {bi}")
            pt = await self._decrypt_block(manifest, block, passphrase)
            blk_start = bi * bs
            lo = max(offset, blk_start) - blk_start
            hi = min(end, blk_start + bs) - blk_start
            result.append(pt[lo:hi])
        return b"".join(result)

    async def put_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        passphrase: str,
        *,
        size_hint: int = 0,
        mode: int = 0o100644,
    ) -> Manifest:
        """Encrypt and scatter data from an async iterator, one block at a time."""
        now = datetime.now(timezone.utc).isoformat()
        parent = self._parent_of(path)
        version = self._index.get_next_version(path)

        manifest = Manifest(
            path=path,
            original_size=0,
            block_size=self._block_size,
            total_blocks=0,
            created_at=now,
            mtime=now, ctime=now, atime=now,
            mode=mode,
            version=version,
            parent_path=parent,
        )

        jid = None
        if self._journal:
            jid = self._journal.begin("put", path, manifest.manifest_id)

        try:
            hasher = hashlib.sha256()
            buffer = bytearray()
            bi = 0
            total_size = 0

            async for chunk_data in stream:
                buffer.extend(chunk_data)
                while len(buffer) >= self._block_size:
                    plaintext_block = bytes(buffer[: self._block_size])
                    del buffer[: self._block_size]
                    hasher.update(plaintext_block)
                    total_size += len(plaintext_block)
                    await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                    bi += 1
                    if self._journal and jid:
                        self._journal.mark_in_progress(
                            jid, {"blocks_written": list(range(bi))}
                        )

            if buffer:
                plaintext_block = bytes(buffer)
                hasher.update(plaintext_block)
                total_size += len(plaintext_block)
                await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                bi += 1
                if self._journal and jid:
                    self._journal.mark_in_progress(
                        jid, {"blocks_written": list(range(bi))}
                    )

            manifest.original_size = total_size
            manifest.total_blocks = bi
            manifest.plaintext_hash = hasher.hexdigest()

            if self._journal and jid:
                with self._index.connection:
                    self._index._save_manifest_raw(manifest)
                    self._journal._commit_raw(jid)
            else:
                self._index.save_manifest(manifest)

        except Exception:
            if self._journal and jid:
                self._journal.rollback(jid)
            raise

        return manifest

    async def get_stream(
        self, path: str, passphrase: str, *, follow_symlinks: bool = True,
    ) -> AsyncIterator[bytes]:
        """Yield decrypted blocks one at a time as an async iterator."""
        manifest = self._resolve_path(path, follow_symlinks)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        if manifest.file_type != "regular":
            raise FileNotFoundError(f"not a regular file: {path}")
        for block in sorted(manifest.blocks, key=lambda b: b.block_index):
            yield await self._decrypt_block(manifest, block, passphrase)

    async def ls(self, path: str = "") -> list[str]:
        """List virtual paths. With *path*, list children of that directory."""
        if path:
            return self._index.list_children(path)
        return self._index.list_paths()

    async def rm(self, path: str) -> bool:
        """Delete a file's chunks from adapters and its manifest from the index."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            return False
        if manifest.file_type == "directory":
            children = self._index.list_children(path)
            if children:
                raise OSError(f"directory not empty: {path}")

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
        new_manifest.parent_path = self._parent_of(dst_path)
        new_manifest.created_at = datetime.now(timezone.utc).isoformat()
        self._index.save_manifest(new_manifest)
        return True

    async def mv(self, src_path: str, dst_path: str) -> bool:
        """Rename a virtual file path."""
        manifest = self._index.get_manifest_by_path(src_path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {src_path}")
        manifest.path = dst_path
        manifest.parent_path = self._parent_of(dst_path)
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

    # --- directories -------------------------------------------------------

    async def mkdir(
        self, path: str, *, mode: int = 0o040755, parents: bool = False,
    ) -> Manifest:
        """Create a directory entry."""
        if parents:
            self._ensure_parents(path)
        now = datetime.now(timezone.utc).isoformat()
        parent = self._parent_of(path)
        manifest = Manifest(
            path=path,
            original_size=0,
            block_size=0,
            total_blocks=0,
            created_at=now,
            file_type="directory",
            mode=mode,
            mtime=now,
            ctime=now,
            atime=now,
            version=self._index.get_next_version(path),
            parent_path=parent,
        )
        self._index.save_manifest(manifest)
        return manifest

    async def rmdir(self, path: str) -> bool:
        """Remove an empty directory."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            return False
        if manifest.file_type != "directory":
            raise OSError(f"not a directory: {path}")
        children = self._index.list_children(path)
        if children:
            raise OSError(f"directory not empty: {path}")
        self._index.delete_manifest(manifest.manifest_id)
        return True

    async def tree(self, path: str = "/") -> dict:
        """Return a nested dict representing the directory tree."""
        children = self._index.list_children(path)
        result: dict = {"path": path, "children": []}
        for child in children:
            m = self._index.get_manifest_by_path(child)
            if m and m.file_type == "directory":
                result["children"].append(await self.tree(child))
            else:
                result["children"].append({"path": child})
        return result

    def _ensure_parents(self, path: str) -> None:
        """Create directory manifests for all missing ancestors of *path*."""
        parts = path.strip("/").split("/")
        for i in range(len(parts) - 1):
            dir_path = "/" + "/".join(parts[: i + 1])
            existing = self._index.get_manifest_by_path(dir_path)
            if existing is None:
                now = datetime.now(timezone.utc).isoformat()
                parent = self._parent_of(dir_path)
                m = Manifest(
                    path=dir_path,
                    original_size=0,
                    block_size=0,
                    total_blocks=0,
                    created_at=now,
                    file_type="directory",
                    mode=0o040755,
                    mtime=now,
                    ctime=now,
                    atime=now,
                    version=self._index.get_next_version(dir_path),
                    parent_path=parent,
                )
                self._index.save_manifest(m)

    # --- symlinks ----------------------------------------------------------

    async def symlink(self, link_path: str, target_path: str) -> Manifest:
        """Create a symbolic link at *link_path* pointing to *target_path*."""
        now = datetime.now(timezone.utc).isoformat()
        parent = self._parent_of(link_path)
        manifest = Manifest(
            path=link_path,
            original_size=0,
            block_size=0,
            total_blocks=0,
            created_at=now,
            file_type="symlink",
            mode=0o120777,
            symlink_target=target_path,
            mtime=now,
            ctime=now,
            atime=now,
            version=self._index.get_next_version(link_path),
            parent_path=parent,
        )
        self._index.save_manifest(manifest)
        return manifest

    async def readlink(self, path: str) -> str:
        """Return the target of a symlink."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        if manifest.file_type != "symlink":
            raise OSError(f"not a symlink: {path}")
        return manifest.symlink_target

    # --- versioning --------------------------------------------------------

    async def get_version(
        self, path: str, version: int, passphrase: str,
    ) -> bytes:
        """Retrieve a specific version of a file."""
        manifest = self._index.get_manifest_by_path_version(path, version)
        if manifest is None:
            raise FileNotFoundError(
                f"no manifest for path {path!r} version {version}"
            )
        parts: list[bytes] = []
        for block in sorted(manifest.blocks, key=lambda b: b.block_index):
            parts.append(await self._decrypt_block(manifest, block, passphrase))
        return b"".join(parts)

    async def list_versions(self, path: str) -> list[dict]:
        """Return version history for a path."""
        return self._index.list_versions(path)

    async def rollback(self, path: str, version: int) -> Manifest:
        """Restore a previous version by creating a new manifest with its blocks."""
        old = self._index.get_manifest_by_path_version(path, version)
        if old is None:
            raise FileNotFoundError(
                f"no manifest for path {path!r} version {version}"
            )
        new = copy.deepcopy(old)
        new.manifest_id = uuid.uuid4().hex
        new.version = self._index.get_next_version(path)
        new.created_at = datetime.now(timezone.utc).isoformat()
        new.mtime = new.created_at
        self._index.save_manifest(new)
        return new

    # --- recovery ----------------------------------------------------------

    async def recover(self) -> dict:
        """Clean up incomplete journal entries from crashed operations."""
        if self._journal is None:
            return {"recovered": 0, "cleaned_chunks": 0}
        entries = self._journal.get_incomplete()
        cleaned_chunks = 0
        for entry in entries:
            manifest = self._index.get_manifest(entry.manifest_id)
            if manifest is not None:
                for block in manifest.blocks:
                    for cr in block.chunks:
                        try:
                            adapter = self._adapter_by_name(cr.adapter_name)
                            ref = StoredChunkRef(
                                adapter_name=cr.adapter_name, location=cr.location,
                            )
                            await adapter.delete_chunk(ref)
                            cleaned_chunks += 1
                        except Exception:
                            pass
                self._index.delete_manifest(manifest.manifest_id)
            self._journal.rollback(entry.journal_id)
        return {"recovered": len(entries), "cleaned_chunks": cleaned_chunks}

    # --- internal ----------------------------------------------------------

    @staticmethod
    def _parent_of(path: str) -> str:
        parts = path.rsplit("/", 1)
        return parts[0] if len(parts) > 1 and parts[0] else "/"

    def _resolve_path(
        self, path: str, follow_symlinks: bool = True,
    ) -> Manifest | None:
        """Resolve *path*, following symlinks up to 40 levels deep."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None or not follow_symlinks:
            return manifest
        depth = 0
        while manifest is not None and manifest.file_type == "symlink":
            depth += 1
            if depth > 40:
                raise OSError("too many levels of symbolic links")
            manifest = self._index.get_manifest_by_path(manifest.symlink_target)
        return manifest

    async def _decrypt_block(
        self, manifest: Manifest, block: BlockRecord, passphrase: str,
    ) -> bytes:
        """Decrypt a single block, using the block cache if available."""
        if self._block_cache is not None:
            cached = self._block_cache.get(manifest.manifest_id, block.block_index)
            if cached is not None:
                return cached

        raw_chunks: list[Chunk] = []
        for cr in block.chunks:
            adapter = self._adapter_by_name(cr.adapter_name)
            ref = StoredChunkRef(
                adapter_name=cr.adapter_name, location=cr.location,
            )
            wire_str = await adapter.read_chunk(ref)
            if self._key_ring:
                sub_key = self._key_ring.get_sub_key(cr.adapter_name)
                encoded = unseal(bytes.fromhex(wire_str), sub_key)
            else:
                encoded = wire_str
            raw_chunks.append(Chunk.decode(encoded))
        ct = reassemble(raw_chunks)
        if block_digest(ct) != block.digest:
            raise ValueError(
                f"block {block.block_index} digest mismatch — data may be corrupted"
            )
        pt = decrypt(ct, passphrase)

        if self._block_cache is not None:
            self._block_cache.put(manifest.manifest_id, block.block_index, pt)

        return pt

    async def _scatter_block(
        self, manifest: Manifest, bi: int, plaintext_block: bytes, passphrase: str,
    ) -> None:
        """Encrypt, chunk, and scatter a single block, appending to manifest."""
        ct = encrypt(plaintext_block, passphrase)
        digest = block_digest(ct)
        block = BlockRecord(block_index=bi, digest=digest, size_bytes=len(ct))
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

    def _pick_adapter(self) -> Adapter:
        """Round-robin across all adapters."""
        adapter = self._adapters[self._next_adapter % len(self._adapters)]
        self._next_adapter += 1
        return adapter

    def _adapter_by_name(self, name: str) -> Adapter:
        for a in self._adapters:
            if a.profile().name == name:
                return a
        raise LookupError(f"no adapter named {name!r}")
