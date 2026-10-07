"""Filesystem check — verify, detect, and repair PICARD virtual filesystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from picard.adapters.base import Adapter, StoredChunkRef
from picard.chunker import Chunk, reassemble
from picard.envelope import unseal
from picard.index import Index
from picard.journal import Journal
from picard.keys import KeyRing
from picard.manifest import block_digest


@dataclass
class FsckReport:
    total_manifests: int = 0
    total_blocks: int = 0
    total_chunks: int = 0
    verified_chunks: int = 0
    missing_chunks: int = 0
    corrupt_blocks: int = 0
    orphan_chunks: int = 0
    directory_errors: int = 0
    symlink_errors: int = 0
    version_gaps: int = 0
    journal_incomplete: int = 0
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.missing_chunks == 0
            and self.corrupt_blocks == 0
            and self.directory_errors == 0
            and self.symlink_errors == 0
        )


async def fsck(
    index: Index,
    adapters: Sequence[Adapter],
    *,
    verify_digests: bool = True,
    check_directories: bool = True,
    check_symlinks: bool = True,
    check_journal: bool = True,
    key_ring: KeyRing | None = None,
) -> FsckReport:
    report = FsckReport()
    adapter_map = {a.profile().name: a for a in adapters}

    paths = index.list_paths()
    manifests = []
    for p in paths:
        m = index.get_manifest_by_path(p)
        if m is not None:
            manifests.append(m)

    report.total_manifests = len(manifests)

    for m in manifests:
        report.total_blocks += len(m.blocks)
        for block in m.blocks:
            report.total_chunks += len(block.chunks)

            if verify_digests:
                raw_chunks: list[Chunk] = []
                block_ok = True
                for cr in block.chunks:
                    adapter = adapter_map.get(cr.adapter_name)
                    if adapter is None:
                        report.missing_chunks += 1
                        report.errors.append({
                            "type": "missing_adapter",
                            "path": m.path,
                            "block_index": block.block_index,
                            "adapter": cr.adapter_name,
                        })
                        block_ok = False
                        continue
                    try:
                        ref = StoredChunkRef(
                            adapter_name=cr.adapter_name,
                            location=cr.location,
                        )
                        wire_str = await adapter.read_chunk(ref)
                        if key_ring:
                            sub_key = key_ring.get_sub_key(cr.adapter_name)
                            encoded = unseal(bytes.fromhex(wire_str), sub_key)
                        else:
                            encoded = wire_str
                        raw_chunks.append(Chunk.decode(encoded))
                        report.verified_chunks += 1
                    except Exception as e:
                        report.missing_chunks += 1
                        report.errors.append({
                            "type": "missing_chunk",
                            "path": m.path,
                            "block_index": block.block_index,
                            "chunk_index": cr.chunk_index,
                            "error": str(e),
                        })
                        block_ok = False

                if block_ok and raw_chunks:
                    ct = reassemble(raw_chunks)
                    if block_digest(ct) != block.digest:
                        report.corrupt_blocks += 1
                        report.errors.append({
                            "type": "corrupt_block",
                            "path": m.path,
                            "block_index": block.block_index,
                        })

    if check_directories:
        for m in manifests:
            if m.file_type == "directory":
                continue
            if m.parent_path and m.parent_path != "/":
                parent = index.get_manifest_by_path(m.parent_path)
                if parent is None:
                    report.directory_errors += 1
                    report.errors.append({
                        "type": "missing_parent",
                        "path": m.path,
                        "parent_path": m.parent_path,
                    })
                elif parent.file_type != "directory":
                    report.directory_errors += 1
                    report.errors.append({
                        "type": "parent_not_directory",
                        "path": m.path,
                        "parent_path": m.parent_path,
                    })

    if check_symlinks:
        for m in manifests:
            if m.file_type != "symlink":
                continue
            if not m.symlink_target:
                report.symlink_errors += 1
                report.errors.append({
                    "type": "empty_symlink_target",
                    "path": m.path,
                })
            else:
                target = index.get_manifest_by_path(m.symlink_target)
                if target is None:
                    report.symlink_errors += 1
                    report.errors.append({
                        "type": "dangling_symlink",
                        "path": m.path,
                        "target": m.symlink_target,
                    })

    if check_journal:
        try:
            journal = Journal(index.connection)
            incomplete = journal.get_incomplete()
            report.journal_incomplete = len(incomplete)
            for entry in incomplete:
                report.errors.append({
                    "type": "incomplete_journal",
                    "journal_id": entry.journal_id,
                    "operation": entry.operation,
                    "path": entry.path,
                    "state": entry.state.value,
                })
        except Exception:
            pass

    return report


async def repair(
    index: Index,
    adapters: Sequence[Adapter],
    report: FsckReport,
    *,
    fix_statuses: bool = True,
    remove_orphans: bool = False,
) -> dict:
    """Apply fixes based on an fsck report."""
    fixed = 0
    removed = 0

    if fix_statuses:
        for err in report.errors:
            if err["type"] == "incomplete_journal":
                try:
                    journal = Journal(index.connection)
                    journal.rollback(err["journal_id"])
                    fixed += 1
                except Exception:
                    pass

    return {"fixed": fixed, "removed": removed}
