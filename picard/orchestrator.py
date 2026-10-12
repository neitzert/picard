"""Orchestrator — the flock's shared instinct.

Coordinates encryption, chunking, scattering across adapters,
and the reverse path: gathering, verifying, reassembling, decrypting.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import logging
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
        adapter_timeout: float = 30.0,
    ) -> None:
        if not adapters:
            raise ValueError("need at least one adapter")
        self._adapters = list(adapters)
        self._adapter_map: dict[str, Adapter] = {
            a.profile().name: a for a in self._adapters
        }
        self._index = index
        self._block_size = block_size
        self._key_ring = key_ring
        self._block_cache = block_cache
        self._journal = journal
        self._next_adapter: int = 0
        self._adapter_timeout = adapter_timeout

    async def close(self) -> None:
        """Close all adapters and release resources."""
        await asyncio.gather(
            *(a.close() for a in self._adapters), return_exceptions=True,
        )
        if self._block_cache is not None:
            self._block_cache.clear()

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
            if self._journal and jid:
                for bi in range(total_blocks):
                    start = bi * self._block_size
                    end = start + self._block_size
                    plaintext_block = data[start:end]
                    await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                    self._journal.mark_in_progress(jid, {"last_block": bi})
            elif total_blocks > 1:
                sem = asyncio.Semaphore(8)
                async def _guarded_scatter(bi: int) -> None:
                    start = bi * self._block_size
                    end = start + self._block_size
                    async with sem:
                        await self._scatter_block(
                            manifest, bi, data[start:end], passphrase,
                        )
                await asyncio.gather(
                    *(_guarded_scatter(bi) for bi in range(total_blocks))
                )
                manifest.blocks.sort(key=lambda b: b.block_index)
            else:
                for bi in range(total_blocks):
                    start = bi * self._block_size
                    end = start + self._block_size
                    await self._scatter_block(manifest, bi, data[start:end], passphrase)

            if self._journal and jid:
                with self._index.write_lock, self._index.connection:
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
        blocks = sorted(manifest.blocks, key=lambda b: b.block_index)
        if len(blocks) <= 1:
            parts = [await self._decrypt_block(manifest, b, passphrase) for b in blocks]
        else:
            parts = list(await asyncio.gather(
                *(self._decrypt_block(manifest, b, passphrase) for b in blocks)
            ))
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
        needed = list(range(start_bi, end_bi + 1))
        for bi in needed:
            if bi not in blocks_by_idx:
                raise ValueError(f"missing block {bi}")

        if len(needed) <= 1:
            decrypted = [await self._decrypt_block(
                manifest, blocks_by_idx[needed[0]], passphrase
            )] if needed else []
        else:
            decrypted = list(await asyncio.gather(*(
                self._decrypt_block(manifest, blocks_by_idx[bi], passphrase)
                for bi in needed
            )))

        result: list[bytes] = []
        for i, bi in enumerate(needed):
            pt = decrypted[i]
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
            buf_offset = 0
            bi = 0
            total_size = 0

            async for chunk_data in stream:
                buffer.extend(chunk_data)
                while len(buffer) - buf_offset >= self._block_size:
                    plaintext_block = bytes(
                        buffer[buf_offset : buf_offset + self._block_size]
                    )
                    buf_offset += self._block_size
                    hasher.update(plaintext_block)
                    total_size += len(plaintext_block)
                    await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                    bi += 1
                    if self._journal and jid:
                        self._journal.mark_in_progress(
                            jid, {"last_block": bi - 1}
                        )

            if buf_offset > 0:
                buffer = bytearray(buffer[buf_offset:])
                buf_offset = 0
            if buffer:
                plaintext_block = bytes(buffer)
                hasher.update(plaintext_block)
                total_size += len(plaintext_block)
                await self._scatter_block(manifest, bi, plaintext_block, passphrase)
                bi += 1
                if self._journal and jid:
                    self._journal.mark_in_progress(
                        jid, {"last_block": bi - 1}
                    )

            manifest.original_size = total_size
            manifest.total_blocks = bi
            manifest.plaintext_hash = hasher.hexdigest()

            if self._journal and jid:
                with self._index.write_lock, self._index.connection:
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
        """Yield decrypted blocks one at a time with 1-block lookahead."""
        manifest = self._resolve_path(path, follow_symlinks)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")
        if manifest.file_type != "regular":
            raise FileNotFoundError(f"not a regular file: {path}")
        blocks = sorted(manifest.blocks, key=lambda b: b.block_index)
        if not blocks:
            return
        pending: asyncio.Task | None = asyncio.ensure_future(
            self._decrypt_block(manifest, blocks[0], passphrase)
        )
        for i, block in enumerate(blocks):
            current = await pending
            if i + 1 < len(blocks):
                pending = asyncio.ensure_future(
                    self._decrypt_block(manifest, blocks[i + 1], passphrase)
                )
            else:
                pending = None
            yield current

    async def ls(self, path: str = "") -> list[str]:
        """List virtual paths. With *path*, list children of that directory."""
        if path:
            return self._index.list_children(path)
        return self._index.list_paths()

    async def find(self, prefix: str) -> list[str]:
        """Find paths matching a prefix."""
        return self._index.list_paths_with_prefix(prefix)

    async def rm(self, path: str) -> bool:
        """Delete a file's chunks from adapters and its manifest from the index."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            return False
        if manifest.file_type == "directory":
            children = self._index.list_children(path)
            if children:
                raise OSError(f"directory not empty: {path}")

        delete_coros = []
        chunk_refs = []
        for block in manifest.blocks:
            for cr in block.chunks:
                adapter = self._adapter_by_name(cr.adapter_name)
                ref = StoredChunkRef(adapter_name=cr.adapter_name, location=cr.location)
                delete_coros.append(adapter.delete_chunk(ref))
                chunk_refs.append(cr)
        if delete_coros:
            results = await asyncio.gather(*delete_coros, return_exceptions=True)
            failed = [
                {"adapter": chunk_refs[i].adapter_name, "location": chunk_refs[i].location,
                 "error": str(results[i])}
                for i in range(len(results))
                if isinstance(results[i], Exception)
            ]
            if failed:
                logging.getLogger(__name__).warning(
                    "rm %s: %d chunk deletes failed, manifest retained", path, len(failed),
                )
                raise OSError(
                    f"rm {path}: {len(failed)} chunk delete(s) failed — "
                    f"manifest retained to prevent orphans"
                )

        self._index.delete_manifest(manifest.manifest_id)
        return True

    async def info(self, path: str) -> dict:
        """Return detailed metadata about a stored file."""
        manifest = self._index.get_manifest_by_path(path)
        if manifest is None:
            raise FileNotFoundError(f"no manifest for path: {path}")

        adapter_distribution, total_chunk_bytes = (
            self._index.chunk_stats_for_manifest(manifest.manifest_id)
        )

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

        async def _verify_block(block: BlockRecord) -> dict:
            block_report: dict = {
                "verified": 0, "missing": 0, "corrupt": False, "errors": [],
            }

            async def _read_cr(cr):
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
                return cr, Chunk.decode(encoded)

            results = await asyncio.gather(
                *(_read_cr(cr) for cr in block.chunks),
                return_exceptions=True,
            )
            raw_chunks: list[Chunk] = []
            block_ok = True
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    cr = block.chunks[i]
                    block_report["missing"] += 1
                    block_report["errors"].append({
                        "block_index": block.block_index,
                        "chunk_index": cr.chunk_index,
                        "adapter": cr.adapter_name,
                        "error": str(result),
                    })
                    block_ok = False
                else:
                    _, chunk = result
                    raw_chunks.append(chunk)
                    block_report["verified"] += 1

            if block_ok and raw_chunks:
                ct = reassemble(raw_chunks)
                if not hmac.compare_digest(block_digest(ct), block.digest):
                    block_report["corrupt"] = True
                    block_report["errors"].append({
                        "block_index": block.block_index,
                        "error": "block digest mismatch",
                    })
            return block_report

        blocks = sorted(manifest.blocks, key=lambda b: b.block_index)
        block_reports = await asyncio.gather(
            *(_verify_block(b) for b in blocks)
        )
        for br in block_reports:
            report["verified_chunks"] += br["verified"]
            report["missing_chunks"] += br["missing"]
            if br["corrupt"]:
                report["corrupt_blocks"] += 1
            report["errors"].extend(br["errors"])

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
        adapter_stats, total_files = self._index.du_stats()
        total_bytes = sum(s["bytes"] for s in adapter_stats.values())
        total_chunks = sum(s["chunk_count"] for s in adapter_stats.values())

        relevant = [
            a for a in self._adapters if a.profile().name in adapter_stats
        ]
        if relevant:
            async def _cap(a):
                return a.profile().name, await a.capacity()
            results = await asyncio.gather(
                *(_cap(a) for a in relevant), return_exceptions=True,
            )
            for result in results:
                if not isinstance(result, Exception):
                    name, cap = result
                    adapter_stats[name]["capacity"] = cap

        return {
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "total_files": total_files,
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

    async def tree(self, path: str = "/", *, max_depth: int = 64) -> dict:
        """Return a nested dict representing the directory tree."""
        flat = self._index.tree_query(path, max_depth)
        nodes: dict[str, dict] = {path: {"path": path, "children": []}}
        for entry in flat:
            node: dict = {"path": entry["path"]}
            if entry["file_type"] == "directory":
                node["children"] = []
            nodes[entry["path"]] = node
            parent = entry["parent_path"]
            if parent in nodes:
                nodes[parent].setdefault("children", []).append(node)
            else:
                nodes[path].setdefault("children", []).append(node)
        return nodes[path]

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
        blocks = sorted(manifest.blocks, key=lambda b: b.block_index)
        if len(blocks) <= 1:
            parts = [await self._decrypt_block(manifest, b, passphrase) for b in blocks]
        else:
            parts = list(await asyncio.gather(
                *(self._decrypt_block(manifest, b, passphrase) for b in blocks)
            ))
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
                delete_coros = []
                for block in manifest.blocks:
                    for cr in block.chunks:
                        try:
                            adapter = self._adapter_by_name(cr.adapter_name)
                            ref = StoredChunkRef(
                                adapter_name=cr.adapter_name, location=cr.location,
                            )
                            delete_coros.append(adapter.delete_chunk(ref))
                        except Exception:
                            pass
                if delete_coros:
                    results = await asyncio.gather(
                        *delete_coros, return_exceptions=True,
                    )
                    cleaned_chunks += sum(
                        1 for r in results if not isinstance(r, Exception)
                    )
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

        async def _fetch_chunk(cr):
            adapter = self._adapter_by_name(cr.adapter_name)
            ref = StoredChunkRef(
                adapter_name=cr.adapter_name, location=cr.location,
            )
            wire_str = await asyncio.wait_for(
                adapter.read_chunk(ref), timeout=self._adapter_timeout
            )
            if self._key_ring:
                sub_key = self._key_ring.get_sub_key(cr.adapter_name)
                encoded = unseal(bytes.fromhex(wire_str), sub_key)
            else:
                encoded = wire_str
            return Chunk.decode(encoded)

        if len(block.chunks) == 1:
            raw_chunks = [await _fetch_chunk(block.chunks[0])]
        else:
            raw_chunks = list(
                await asyncio.gather(*(_fetch_chunk(cr) for cr in block.chunks))
            )
        ct = reassemble(raw_chunks)
        if not hmac.compare_digest(block_digest(ct), block.digest):
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

        async def _write_one(chunk):
            encoded = chunk.encode()
            if self._key_ring:
                sub_key = self._key_ring.get_sub_key(prof.name)
                wire = seal(encoded, sub_key, manifest.manifest_id)
                wire_str = wire.hex()
            else:
                wire_str = encoded
            ref = await asyncio.wait_for(
                adapter.write_chunk(wire_str), timeout=self._adapter_timeout
            )
            chunk_digest = hashlib.sha256(
                wire_str.encode() if isinstance(wire_str, str) else wire_str
            ).hexdigest()
            return ChunkRecord(
                chunk_index=chunk.chunk_index,
                adapter_name=ref.adapter_name,
                location=ref.location,
                status=ChunkStatus.STORED,
                size_bytes=len(wire_str),
                digest=chunk_digest,
            )

        if len(chunks) == 1:
            block.chunks.append(await _write_one(chunks[0]))
        else:
            records = await asyncio.gather(*(_write_one(c) for c in chunks))
            block.chunks.extend(sorted(records, key=lambda r: r.chunk_index))
        manifest.blocks.append(block)

    def _pick_adapter(self) -> Adapter:
        """Round-robin across all adapters."""
        adapter = self._adapters[self._next_adapter % len(self._adapters)]
        self._next_adapter += 1
        return adapter

    def _adapter_by_name(self, name: str) -> Adapter:
        adapter = self._adapter_map.get(name)
        if adapter is None:
            raise LookupError(f"no adapter named {name!r}")
        return adapter
