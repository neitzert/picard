"""CLI — ``picard put / get / ls / rm / demo``.

A thin shell over the orchestrator that wires up adapters, index,
and passphrase handling.  Reads ``~/.picard/config.toml`` for
adapter configuration; falls back to a single filesystem adapter.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from picard.adapters.filesystem import FilesystemAdapter
from picard.config import load_config, resolve_params, DEFAULT_CONFIG_PATH
from picard.index import Index
from picard.keys import KeyRing, load_master_key
from picard.orchestrator import Orchestrator
from picard.registry import get_adapter_class, instantiate, list_adapter_types

app = typer.Typer(
    name="picard",
    help="Scatter encrypted data across protocol gaps.",
    add_completion=False,
)

_DEFAULT_STORE = Path.home() / ".picard" / "chunks"
_DEFAULT_DB = Path.home() / ".picard" / "index.db"


def _get_passphrase() -> str:
    """Prompt for a passphrase (no echo)."""
    import getpass

    pp = getpass.getpass("Passphrase: ")
    if not pp:
        typer.echo("passphrase must not be empty", err=True)
        raise typer.Exit(1)
    return pp


def _adapters_from_config(store_dir: Path) -> list:
    """Build adapter instances from config.toml, or fall back to filesystem."""
    cfg = load_config()
    adapters = []
    for entry in cfg.adapters:
        if not entry.enabled:
            continue
        try:
            params = resolve_params(entry.params)
            adapters.append(instantiate(entry.type, **params))
        except Exception:
            pass
    if not adapters:
        store_dir.mkdir(parents=True, exist_ok=True)
        adapters.append(FilesystemAdapter(store_dir))
    return adapters


def _make_orchestrator(
    store_dir: Path = _DEFAULT_STORE,
    db_path: Path = _DEFAULT_DB,
    keyfile: Path | None = None,
    passphrase: str | None = None,
) -> Orchestrator:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    adapters = _adapters_from_config(store_dir)
    index = Index(db_path)

    key_ring = None
    if keyfile or passphrase:
        mk = load_master_key(passphrase=passphrase, keyfile=keyfile)
        key_ring = KeyRing(mk)

    return Orchestrator(adapters, index, key_ring=key_ring)


@app.command()
def put(
    file: Path = typer.Argument(..., help="Local file to store."),
    virtual_path: str = typer.Option(
        "", "--as", "-a", help="Virtual path (default: filename)."
    ),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file for sub-key derivation."
    ),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Encrypt and scatter a file."""
    if not file.is_file():
        typer.echo(f"not a file: {file}", err=True)
        raise typer.Exit(1)
    data = file.read_bytes()
    vpath = virtual_path or file.name
    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    manifest = asyncio.run(orch.put(vpath, data, pp))
    typer.echo(
        f"stored {vpath}  ({manifest.total_blocks} blocks, "
        f"{manifest.total_chunks} chunks, manifest={manifest.manifest_id})"
    )


@app.command()
def get(
    virtual_path: str = typer.Argument(..., help="Virtual path to retrieve."),
    output: Path = typer.Option(
        None, "--out", "-o", help="Write to file (default: stdout)."
    ),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file for sub-key derivation."
    ),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Retrieve and decrypt a file."""
    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    data = asyncio.run(orch.get(virtual_path, pp))
    if output:
        output.write_bytes(data)
        typer.echo(f"written to {output}")
    else:
        sys.stdout.buffer.write(data)


@app.command(name="ls")
def list_files(
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """List all stored virtual paths."""
    orch = _make_orchestrator(store_dir, db_path)
    paths = asyncio.run(orch.ls())
    if not paths:
        typer.echo("(empty)")
    for p in paths:
        typer.echo(p)


@app.command()
def rm(
    virtual_path: str = typer.Argument(..., help="Virtual path to remove."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Delete a file's chunks and index entry."""
    orch = _make_orchestrator(store_dir, db_path)
    removed = asyncio.run(orch.rm(virtual_path))
    if removed:
        typer.echo(f"removed {virtual_path}")
    else:
        typer.echo(f"not found: {virtual_path}", err=True)
        raise typer.Exit(1)


@app.command()
def info(
    virtual_path: str = typer.Argument(..., help="Virtual path to inspect."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show detailed info about a stored file."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.info(virtual_path))
    typer.echo(f"Path:           {result['path']}")
    typer.echo(f"Manifest ID:    {result['manifest_id']}")
    typer.echo(f"Original size:  {result['original_size']} bytes")
    typer.echo(f"Block size:     {result['block_size']} bytes")
    typer.echo(f"Blocks:         {result['total_blocks']}")
    typer.echo(f"Chunks:         {result['total_chunks']}")
    typer.echo(f"Stored bytes:   {result['total_chunk_bytes']}")
    typer.echo(f"Created:        {result['created_at']}")
    if result.get("plaintext_hash"):
        typer.echo(f"SHA-256:        {result['plaintext_hash']}")
    typer.echo(f"All stored:     {result['all_stored']}")
    if result["adapter_distribution"]:
        typer.echo("Adapters:")
        for name, count in result["adapter_distribution"].items():
            typer.echo(f"  {name}: {count} chunks")


@app.command()
def verify(
    virtual_path: str = typer.Argument(..., help="Virtual path to verify."),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file for sub-key derivation."
    ),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Verify integrity of all chunks for a stored file."""
    pp = None
    if keyfile:
        pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    report = asyncio.run(orch.verify(virtual_path))
    typer.echo(f"Verify: {report['path']}")
    typer.echo(f"  Blocks: {report['total_blocks']}, Chunks: {report['total_chunks']}")
    typer.echo(f"  Verified: {report['verified_chunks']}")
    if report["missing_chunks"]:
        typer.echo(f"  MISSING:  {report['missing_chunks']}", err=True)
    if report["corrupt_blocks"]:
        typer.echo(f"  CORRUPT:  {report['corrupt_blocks']}", err=True)
    for err in report["errors"]:
        typer.echo(f"  ! {err}", err=True)
    if report["ok"]:
        typer.echo("  OK")
    else:
        raise typer.Exit(1)


@app.command(name="export")
def export_cmd(
    virtual_path: str = typer.Argument(..., help="Virtual path to export."),
    output: Path = typer.Option(
        None, "--out", "-o", help="Write bundle to file."
    ),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file."
    ),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Export an encrypted manifest bundle for sharing."""
    from picard.bundle import export_bundle

    pp = _get_passphrase()
    mk = load_master_key(passphrase=pp, keyfile=keyfile)
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    manifest = orch._index.get_manifest_by_path(virtual_path)
    if manifest is None:
        typer.echo(f"not found: {virtual_path}", err=True)
        raise typer.Exit(1)
    bundle_str = export_bundle(manifest, mk, key_ring=orch._key_ring)
    if output:
        output.write_text(bundle_str, encoding="utf-8")
        typer.echo(f"bundle written to {output}")
    else:
        typer.echo(bundle_str)


@app.command(name="import")
def import_cmd(
    bundle_file: Path = typer.Argument(..., help="Bundle file to import."),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file."
    ),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Import a manifest bundle from another PICARD instance."""
    from picard.bundle import import_bundle

    bundle_str = bundle_file.read_text(encoding="utf-8")
    pp = _get_passphrase()
    manifest, adapter_hints, key_versions = import_bundle(
        bundle_str, passphrase=pp, keyfile=keyfile
    )
    orch = _make_orchestrator(store_dir, db_path)
    orch._index.save_manifest(manifest)
    typer.echo(f"imported {manifest.path} (manifest={manifest.manifest_id})")
    typer.echo(f"  {manifest.total_blocks} blocks, {manifest.total_chunks} chunks")
    if adapter_hints:
        typer.echo("  adapters needed:")
        for name, count in adapter_hints.items():
            typer.echo(f"    {name}: {count} chunks")


@app.command()
def cp(
    src: str = typer.Argument(..., help="Source virtual path."),
    dst: str = typer.Argument(..., help="Destination virtual path."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Copy a virtual file to a new path."""
    orch = _make_orchestrator(store_dir, db_path)
    asyncio.run(orch.cp(src, dst))
    typer.echo(f"copied {src} -> {dst}")


@app.command()
def mv(
    src: str = typer.Argument(..., help="Source virtual path."),
    dst: str = typer.Argument(..., help="Destination virtual path."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Rename a virtual file."""
    orch = _make_orchestrator(store_dir, db_path)
    asyncio.run(orch.mv(src, dst))
    typer.echo(f"moved {src} -> {dst}")


@app.command()
def du(
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show storage usage summary."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.du())
    typer.echo(f"Files:   {result['total_files']}")
    typer.echo(f"Chunks:  {result['total_chunks']}")
    typer.echo(f"Bytes:   {result['total_bytes']}")
    if result["adapters"]:
        typer.echo("Per adapter:")
        for name, stats in result["adapters"].items():
            line = f"  {name}: {stats['chunk_count']} chunks, {stats['bytes']} bytes"
            cap = stats.get("capacity", {})
            if cap.get("free_bytes") is not None:
                free_mb = cap["free_bytes"] / (1024 * 1024)
                line += f" (free: {free_mb:.0f} MiB)"
            typer.echo(line)


@app.command()
def adapters(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show constructor params."),
) -> None:
    """List all registered adapter types."""
    types = list_adapter_types()
    typer.echo(f"{len(types)} adapters registered:\n")
    for t in types:
        if verbose:
            try:
                cls = get_adapter_class(t)
                import inspect
                sig = inspect.signature(cls.__init__)
                params = [p for p in sig.parameters if p != "self"]
                typer.echo(f"  {t:30s}  {cls.__name__}({', '.join(params)})")
            except Exception:
                typer.echo(f"  {t}")
        else:
            typer.echo(f"  {t}")


@app.command()
def demo(
    file: Path = typer.Argument(None, help="File to scatter (default: built-in sample)."),
    adapter_count: int = typer.Option(0, "--adapters", "-n", help="Number of adapters (0=all available)."),
) -> None:
    """Scatter a file across every available protocol adapter.

    The absurd proof-of-concept: one file, 200+ protocols, zero network calls.
    Uses in-memory adapter instances that simulate each protocol's behavior.
    """
    asyncio.run(_demo_async(file, adapter_count))


async def _demo_async(file: Path | None, adapter_count: int) -> None:
    from picard.adapters.memory import MemoryAdapter

    if file and file.is_file():
        data = file.read_bytes()
        name = file.name
    else:
        data = (
            b"PICARD PROOF OF CONCEPT\n"
            b"======================\n\n"
            b"This data was encrypted with AES-256-GCM, chunked,\n"
            b"and scattered across every protocol adapter PICARD knows.\n\n"
            b"If you are reading this, the reassembly worked.\n"
            b"Your firewall didn't help.\n"
        )
        name = "picard-demo.txt"

    all_types = list_adapter_types()
    if adapter_count > 0:
        all_types = all_types[:adapter_count]

    typer.echo(f"\n{'='*70}")
    typer.echo(f"  PICARD — Protocol-Introspecting Chunk Adapter for Resilient Data")
    typer.echo(f"{'='*70}")
    typer.echo(f"\n  Payload:     {name} ({len(data)} bytes)")
    typer.echo(f"  Adapters:    {len(all_types)} protocols")
    typer.echo(f"  Encryption:  AES-256-GCM + Argon2id KDF")
    typer.echo(f"  Chunking:    HKDF-SHA256 sub-keys per adapter")
    typer.echo(f"\n  Scattering...\n")

    adapters = [MemoryAdapter(name=t) for t in all_types]

    # Pad payload so every adapter gets at least one chunk
    if len(data) < len(adapters):
        pad_line = b"\nProtocol security > platform security. " * (len(adapters) // 40 + 1)
        data = data + pad_line[: len(adapters) - len(data)]

    passphrase = "picard-demo-passphrase-not-secret"
    index = Index(":memory:")
    orch = Orchestrator(adapters, index, block_size=1)

    manifest = await orch.put(name, data, passphrase)

    chunk_map: list[tuple[int, int, str]] = []
    for block in manifest.blocks:
        for cr in block.chunks:
            chunk_map.append((block.block_index, cr.chunk_index, cr.adapter_name))

    protocols_used = sorted(set(name for _, _, name in chunk_map))

    typer.echo(f"  {'Block':>5}  {'Chunk':>5}  Protocol")
    typer.echo(f"  {'─'*5}  {'─'*5}  {'─'*50}")
    for bi, ci, aname in chunk_map:
        typer.echo(f"  {bi:>5}  {ci:>5}  {aname}")

    typer.echo(f"\n  Scattered {manifest.total_chunks} chunks across {len(protocols_used)} protocols.")

    typer.echo(f"\n  Reassembling...\n")

    recovered = await orch.get(name, passphrase)
    ok = recovered == data

    if ok:
        typer.echo(f"  ✓ Reassembly successful — {len(recovered)} bytes, SHA-256 verified.")
    else:
        typer.echo(f"  ✗ Reassembly FAILED.", err=True)
        raise typer.Exit(1)

    report = await orch.verify(name)
    typer.echo(f"  ✓ Integrity check: {report['verified_chunks']}/{report['total_chunks']} chunks verified.")

    typer.echo(f"\n{'='*70}")
    typer.echo(f"  ATTACK SURFACE REPORT")
    typer.echo(f"{'='*70}")
    typer.echo(f"\n  To intercept this data, a defender would need to monitor:\n")
    for i, proto in enumerate(protocols_used, 1):
        typer.echo(f"  {i:>4}. {proto}")

    typer.echo(f"\n  Total protocols: {len(protocols_used)}")
    typer.echo(f"  Total chunks:    {manifest.total_chunks}")
    typer.echo(f"  Missing ANY one chunk makes the data unrecoverable.")
    typer.echo(f"\n  Platform security is not protocol security.")
    typer.echo(f"{'='*70}\n")


@app.command(name="scatter-report")
def scatter_report(
    virtual_path: str = typer.Argument(..., help="Virtual path to report on."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show the attack surface map for a stored file."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.info(virtual_path))

    typer.echo(f"\nScatter Report: {result['path']}")
    typer.echo(f"{'─'*60}")
    typer.echo(f"  Original size:  {result['original_size']} bytes")
    typer.echo(f"  Blocks:         {result['total_blocks']}")
    typer.echo(f"  Total chunks:   {result['total_chunks']}")
    typer.echo(f"  Stored bytes:   {result['total_chunk_bytes']}")
    typer.echo(f"  SHA-256:        {result['plaintext_hash']}")

    dist = result["adapter_distribution"]
    typer.echo(f"\n  Protocols touched ({len(dist)}):")
    for name, count in sorted(dist.items()):
        typer.echo(f"    {name:30s}  {count} chunks")

    typer.echo(f"\n  A defender must monitor all {len(dist)} protocols simultaneously.")
    typer.echo(f"  Missing any single chunk renders the data unrecoverable.\n")


@app.command()
def init() -> None:
    """Create a default ~/.picard/config.toml."""
    from picard.config import init_config
    try:
        path = init_config()
        typer.echo(f"Created {path}")
        typer.echo(f"Edit it to add adapters, then run: picard adapters")
    except FileExistsError:
        typer.echo(f"Config already exists: {DEFAULT_CONFIG_PATH}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
