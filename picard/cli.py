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
from picard.raid import RaidLevel, RaidPool
from picard.registry import get_adapter_class, instantiate, list_adapter_types
from picard.scatter import build_from_profile, list_profiles, resolve_profile

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
    """Build adapter instances from config.toml, or fall back to filesystem.

    If ``[raid]`` sections are present, named adapters are wrapped in
    RaidPool instances and replaced in the returned list.
    """
    cfg = load_config()
    by_name: dict[str, object] = {}
    adapters: list = []
    for entry in cfg.adapters:
        if not entry.enabled:
            continue
        try:
            params = resolve_params(entry.params)
            adapter = instantiate(entry.type, **params)
            adapters.append(adapter)
            by_name[entry.name] = adapter
        except Exception:
            pass

    if cfg.raid:
        pooled_names: set[str] = set()
        pools: list = []
        for raid_entry in cfg.raid:
            pool_adapters = [by_name[n] for n in raid_entry.adapters if n in by_name]
            if len(pool_adapters) < 2:
                continue
            level = RaidLevel(raid_entry.level)
            pool = RaidPool(pool_adapters, level, mirror_count=raid_entry.mirror_count)
            pools.append(pool)
            pooled_names.update(raid_entry.adapters)
        remaining = [a for a in adapters if not any(
            name in pooled_names for name, obj in by_name.items() if obj is a
        )]
        adapters = pools + remaining

    if not adapters:
        store_dir.mkdir(parents=True, exist_ok=True)
        adapters.append(FilesystemAdapter(store_dir))
    return adapters


def _make_orchestrator(
    store_dir: Path = _DEFAULT_STORE,
    db_path: Path = _DEFAULT_DB,
    keyfile: Path | None = None,
    passphrase: str | None = None,
    profile_name: str | None = None,
) -> Orchestrator:
    from picard.journal import Journal

    db_path.parent.mkdir(parents=True, exist_ok=True)
    adapters = _adapters_from_config(store_dir)
    index = Index(db_path)
    journal = Journal(index.connection)

    if profile_name:
        cfg = load_config()
        profile = resolve_profile(profile_name, cfg.scatter_profiles)
        adapters = build_from_profile(profile, adapters)

    key_ring = None
    if keyfile or passphrase:
        mk = load_master_key(passphrase=passphrase, keyfile=keyfile)
        key_ring = KeyRing(mk)

    return Orchestrator(adapters, index, key_ring=key_ring, journal=journal)


@app.command()
def put(
    file: Path = typer.Argument(..., help="Local file to store."),
    virtual_path: str = typer.Option(
        "", "--as", "-a", help="Virtual path (default: filename)."
    ),
    keyfile: Path = typer.Option(
        None, "--keyfile", "-k", help="Key file for sub-key derivation."
    ),
    profile: str = typer.Option(
        None, "--profile", "-p", help="Scatter profile (stealth/resilient/speed/paranoid or custom)."
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
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp, profile_name=profile)
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


@app.command(name="profiles")
def scatter_profiles_cmd() -> None:
    """List available scatter profiles."""
    cfg = load_config()
    profiles = list_profiles(cfg.scatter_profiles)
    typer.echo(f"{len(profiles)} scatter profiles:\n")
    for p in profiles:
        raid = p.raid_level.upper().replace("RAID", "RAID ")
        typer.echo(f"  {p.name:15s}  {raid:10s}  {p.description}")
        if p.adapter_tags:
            typer.echo(f"  {'':15s}  tags: {', '.join(p.adapter_tags)}")
        if p.adapter_names:
            typer.echo(f"  {'':15s}  adapters: {', '.join(p.adapter_names)}")
    typer.echo(f"\nUse: picard put --profile <name> <file>")


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


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8471, "--port", "-p", help="Port."),
) -> None:
    """Launch the PICARD web dashboard."""
    from picard.web import serve
    typer.echo(f"PICARD dashboard → http://{host}:{port}")
    serve(host=host, port=port)


@app.command()
def mkdir(
    virtual_path: str = typer.Argument(..., help="Directory path to create."),
    parents: bool = typer.Option(False, "-p", "--parents", help="Create parent directories."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Create a virtual directory."""
    orch = _make_orchestrator(store_dir, db_path)
    asyncio.run(orch.mkdir(virtual_path, parents=parents))
    typer.echo(f"mkdir {virtual_path}")


@app.command(name="stat")
def stat_cmd(
    virtual_path: str = typer.Argument(..., help="Path to stat."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show filesystem metadata for a path."""
    orch = _make_orchestrator(store_dir, db_path)
    m = orch._index.get_manifest_by_path(virtual_path)
    if m is None:
        typer.echo(f"not found: {virtual_path}", err=True)
        raise typer.Exit(1)
    typer.echo(f"  Path:    {m.path}")
    typer.echo(f"  Type:    {m.file_type}")
    typer.echo(f"  Mode:    {oct(m.mode)}")
    typer.echo(f"  Size:    {m.original_size}")
    typer.echo(f"  Version: {m.version}")
    typer.echo(f"  UID/GID: {m.uid}/{m.gid}")
    typer.echo(f"  mtime:   {m.mtime}")
    typer.echo(f"  ctime:   {m.ctime}")
    if m.file_type == "symlink":
        typer.echo(f"  Target:  {m.symlink_target}")


@app.command()
def tree(
    virtual_path: str = typer.Argument("/", help="Root path for tree."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show directory tree."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.tree(virtual_path))
    _print_tree(result, "")


def _print_tree(node: dict, prefix: str) -> None:
    name = node["path"].rsplit("/", 1)[-1] or "/"
    typer.echo(f"{prefix}{name}")
    children = node.get("children", [])
    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        connector = "└── " if is_last else "├── "
        if "children" in child:
            typer.echo(f"{prefix}{connector}", nl=False)
            _print_tree(child, prefix + ("    " if is_last else "│   "))
        else:
            leaf = child["path"].rsplit("/", 1)[-1]
            typer.echo(f"{prefix}{connector}{leaf}")


@app.command()
def find(
    pattern: str = typer.Argument("", help="Prefix to search for."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Find paths matching a prefix."""
    orch = _make_orchestrator(store_dir, db_path)
    if pattern:
        paths = orch._index.list_paths_with_prefix(pattern)
    else:
        paths = asyncio.run(orch.ls())
    for p in paths:
        typer.echo(p)


@app.command()
def touch(
    virtual_path: str = typer.Argument(..., help="Path to touch."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Create an empty file or update timestamps."""
    from datetime import datetime, timezone

    orch = _make_orchestrator(store_dir, db_path)
    m = orch._index.get_manifest_by_path(virtual_path)
    now = datetime.now(timezone.utc).isoformat()
    if m:
        orch._index.update_manifest_metadata(m.manifest_id, mtime=now, atime=now)
        typer.echo(f"touched {virtual_path}")
    else:
        pp = _get_passphrase()
        asyncio.run(orch.put(virtual_path, b"", pp))
        typer.echo(f"created {virtual_path}")


@app.command()
def ln(
    target: str = typer.Argument(..., help="Target path."),
    link: str = typer.Argument(..., help="Link path to create."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Create a symbolic link."""
    orch = _make_orchestrator(store_dir, db_path)
    asyncio.run(orch.symlink(link, target))
    typer.echo(f"{link} -> {target}")


@app.command()
def head(
    virtual_path: str = typer.Argument(..., help="Path to read."),
    bytes_count: int = typer.Option(1024, "-c", "--bytes", help="Number of bytes."),
    keyfile: Path = typer.Option(None, "--keyfile", "-k"),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Read the first N bytes of a file."""
    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    data = asyncio.run(orch.read_range(virtual_path, 0, bytes_count, pp))
    sys.stdout.buffer.write(data)


@app.command()
def tail(
    virtual_path: str = typer.Argument(..., help="Path to read."),
    bytes_count: int = typer.Option(1024, "-c", "--bytes", help="Number of bytes."),
    keyfile: Path = typer.Option(None, "--keyfile", "-k"),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Read the last N bytes of a file."""
    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    result = asyncio.run(orch.info(virtual_path))
    size = result["original_size"]
    offset = max(0, size - bytes_count)
    data = asyncio.run(orch.read_range(virtual_path, offset, bytes_count, pp))
    sys.stdout.buffer.write(data)


@app.command(name="fsck")
def fsck_cmd(
    keyfile: Path = typer.Option(None, "--keyfile", "-k"),
    no_digests: bool = typer.Option(False, "--no-digests", help="Skip digest verification."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Check filesystem integrity."""
    from picard.fsck import fsck as _fsck

    pp = None
    if keyfile:
        pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    report = asyncio.run(_fsck(
        orch._index, orch._adapters,
        verify_digests=not no_digests,
        key_ring=orch._key_ring,
    ))
    typer.echo(f"fsck: {report.total_manifests} manifests, "
               f"{report.total_blocks} blocks, {report.total_chunks} chunks")
    typer.echo(f"  Verified:    {report.verified_chunks}")
    if report.missing_chunks:
        typer.echo(f"  Missing:     {report.missing_chunks}", err=True)
    if report.corrupt_blocks:
        typer.echo(f"  Corrupt:     {report.corrupt_blocks}", err=True)
    if report.directory_errors:
        typer.echo(f"  Dir errors:  {report.directory_errors}", err=True)
    if report.symlink_errors:
        typer.echo(f"  Link errors: {report.symlink_errors}", err=True)
    if report.journal_incomplete:
        typer.echo(f"  Journal:     {report.journal_incomplete} incomplete", err=True)
    for err in report.errors:
        typer.echo(f"  ! {err}", err=True)
    if report.ok:
        typer.echo("  OK")
    else:
        raise typer.Exit(1)


@app.command()
def log(
    virtual_path: str = typer.Argument(..., help="Path to show history for."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show version history for a path."""
    orch = _make_orchestrator(store_dir, db_path)
    versions = asyncio.run(orch.list_versions(virtual_path))
    if not versions:
        typer.echo(f"no versions for {virtual_path}")
        return
    for v in versions:
        typer.echo(f"  v{v['version']}  {v['created_at']}  "
                   f"{v['original_size']} bytes  {v['manifest_id'][:12]}...")


@app.command(name="recover")
def recover_cmd(
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Recover from crashed operations using the journal."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.recover())
    typer.echo(f"Recovered {result['recovered']} incomplete operations")
    typer.echo(f"Cleaned {result['cleaned_chunks']} orphan chunks")


@app.command(name="mount")
def mount_cmd(
    mountpoint: str = typer.Argument(..., help="Mount point (directory or drive letter)."),
    keyfile: Path = typer.Option(None, "--keyfile", "-k"),
    readonly: bool = typer.Option(False, "--ro", help="Mount read-only."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Mount PICARD as a FUSE filesystem."""
    from picard.cache import BlockCache
    from picard.fuse_mount import HAS_FUSE, mount as fuse_mount

    if not HAS_FUSE:
        typer.echo(
            "FUSE not available. Install:\n"
            "  Linux: sudo apt install libfuse-dev (or equivalent)\n"
            "  macOS: brew install macfuse\n"
            "  Windows: install WinFSP from https://winfsp.dev",
            err=True,
        )
        raise typer.Exit(1)

    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    cache = BlockCache()
    typer.echo(f"Mounting PICARD at {mountpoint} ...")
    typer.echo("Press Ctrl+C to unmount.")
    fuse_mount(orch, pp, mountpoint, block_cache=cache, readonly=readonly)


if __name__ == "__main__":
    app()
