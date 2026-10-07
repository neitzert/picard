"""CLI — ``picard put / get / ls / rm / demo``.

A thin shell over the orchestrator that wires up adapters, index,
and passphrase handling.  Reads ``~/.picard/config.toml`` for
adapter configuration; falls back to a single filesystem adapter.
"""

from __future__ import annotations

import asyncio
import json as json_mod
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

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

console = Console()
err_console = Console(stderr=True)

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


def _spinner(description: str):
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=err_console,
        disable=not err_console.is_terminal,
    )


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
    with _spinner("Encrypting and scattering...") as progress:
        progress.add_task("Encrypting and scattering...", total=None)
        manifest = asyncio.run(orch.put(vpath, data, pp))
    console.print(
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
    with _spinner("Fetching and decrypting...") as progress:
        progress.add_task("Fetching and decrypting...", total=None)
        data = asyncio.run(orch.get(virtual_path, pp))
    if output:
        output.write_bytes(data)
        console.print(f"written to {output}")
    else:
        sys.stdout.buffer.write(data)


@app.command(name="ls")
def list_files(
    path: str = typer.Argument("", help="Directory path to list (default: all)."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """List stored virtual paths."""
    orch = _make_orchestrator(store_dir, db_path)
    paths = asyncio.run(orch.ls(path))
    if as_json:
        entries = []
        for p in paths:
            m = orch._index.get_manifest_by_path(p)
            entry = {"path": p}
            if m:
                entry.update({
                    "type": m.file_type,
                    "size": m.original_size,
                    "version": m.version,
                })
            entries.append(entry)
        typer.echo(json_mod.dumps(entries, indent=2))
        return
    if not paths:
        typer.echo("(empty)")
        return
    for p in paths:
        m = orch._index.get_manifest_by_path(p)
        if m and m.file_type == "directory":
            console.print(f"[blue]{p}/[/blue]")
        elif m and m.file_type == "symlink":
            console.print(f"[cyan]{p}[/cyan] -> [dim]{m.symlink_target}[/dim]")
        else:
            console.print(p)


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
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show detailed info about a stored file."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.info(virtual_path))
    if as_json:
        typer.echo(json_mod.dumps(result, indent=2))
        return
    console.print(f"[dim]Path:[/dim]           {result['path']}")
    console.print(f"[dim]Manifest ID:[/dim]    {result['manifest_id']}")
    console.print(f"[dim]Original size:[/dim]  {result['original_size']} bytes")
    console.print(f"[dim]Block size:[/dim]     {result['block_size']} bytes")
    console.print(f"[dim]Blocks:[/dim]         {result['total_blocks']}")
    console.print(f"[dim]Chunks:[/dim]         {result['total_chunks']}")
    console.print(f"[dim]Stored bytes:[/dim]   {result['total_chunk_bytes']}")
    console.print(f"[dim]Created:[/dim]        {result['created_at']}")
    if result.get("plaintext_hash"):
        console.print(f"[dim]SHA-256:[/dim]        {result['plaintext_hash']}")
    console.print(f"[dim]All stored:[/dim]     {result['all_stored']}")
    if result["adapter_distribution"]:
        console.print("[dim]Adapters:[/dim]")
        for name, count in result["adapter_distribution"].items():
            console.print(f"  {name}: {count} chunks")


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
    with _spinner("Verifying chunks...") as progress:
        progress.add_task("Verifying chunks...", total=None)
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
        console.print("  [bold green]OK[/bold green]")
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
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show storage usage summary."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.du())
    if as_json:
        typer.echo(json_mod.dumps(result, indent=2))
        return
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
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List all registered adapter types."""
    types = list_adapter_types()
    if as_json:
        typer.echo(json_mod.dumps(sorted(types), indent=2))
        return
    table = Table(
        title=f"Registered Adapters ({len(types)})",
        box=box.SIMPLE,
    )
    table.add_column("Name", style="bold")
    if verbose:
        table.add_column("Class")
        table.add_column("Params")
    for t in sorted(types):
        if verbose:
            try:
                cls = get_adapter_class(t)
                import inspect
                sig = inspect.signature(cls.__init__)
                params = [p for p in sig.parameters if p != "self"]
                table.add_row(t, cls.__name__, ", ".join(params))
            except Exception:
                table.add_row(t, "?", "")
        else:
            table.add_row(t)
    console.print(table)


@app.command(name="profiles")
def scatter_profiles_cmd(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List available scatter profiles."""
    cfg = load_config()
    profiles = list_profiles(cfg.scatter_profiles)
    if as_json:
        data = [
            {
                "name": p.name,
                "raid_level": p.raid_level,
                "description": p.description,
                "adapter_tags": p.adapter_tags,
                "adapter_names": p.adapter_names,
            }
            for p in profiles
        ]
        typer.echo(json_mod.dumps(data, indent=2))
        return
    table = Table(
        title=f"Scatter Profiles ({len(profiles)})",
        box=box.SIMPLE,
    )
    table.add_column("Name", style="bold")
    table.add_column("RAID")
    table.add_column("Description")
    for p in profiles:
        raid = p.raid_level.upper().replace("RAID", "RAID ")
        table.add_row(p.name, raid, p.description)
    console.print(table)
    console.print(f"\nUse: picard put --profile <name> <file>")


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

    console.print(f"\n{'='*70}")
    console.print(f"  [bold]PICARD[/bold] — Protocol-Introspecting Chunk Adapter for Resilient Data")
    console.print(f"{'='*70}")
    console.print(f"\n  Payload:     {name} ({len(data)} bytes)")
    console.print(f"  Adapters:    {len(all_types)} protocols")
    console.print(f"  Encryption:  AES-256-GCM + Argon2id KDF")
    console.print(f"  Chunking:    HKDF-SHA256 sub-keys per adapter")

    adapters = [MemoryAdapter(name=t) for t in all_types]

    if len(data) < len(adapters):
        pad_line = b"\nProtocol security > platform security. " * (len(adapters) // 40 + 1)
        data = data + pad_line[: len(adapters) - len(data)]

    passphrase = "picard-demo-passphrase-not-secret"
    index = Index(":memory:")
    orch = Orchestrator(adapters, index, block_size=1)

    console.print(f"\n  Scattering...\n")
    with _spinner("Scattering across adapters...") as progress:
        progress.add_task("Scattering across adapters...", total=None)
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

    console.print(f"\n  Scattered {manifest.total_chunks} chunks across {len(protocols_used)} protocols.")
    console.print(f"\n  Reassembling...\n")

    with _spinner("Reassembling...") as progress:
        progress.add_task("Reassembling...", total=None)
        recovered = await orch.get(name, passphrase)
    ok = recovered == data

    if ok:
        console.print(f"  [bold green]✓[/bold green] Reassembly successful — {len(recovered)} bytes, SHA-256 verified.")
    else:
        console.print(f"  [bold red]✗[/bold red] Reassembly FAILED.", highlight=False)
        raise typer.Exit(1)

    report = await orch.verify(name)
    console.print(f"  [bold green]✓[/bold green] Integrity check: {report['verified_chunks']}/{report['total_chunks']} chunks verified.")

    console.print(f"\n{'='*70}")
    console.print(f"  [bold]ATTACK SURFACE REPORT[/bold]")
    console.print(f"{'='*70}")
    console.print(f"\n  To intercept this data, a defender would need to monitor:\n")
    for i, proto in enumerate(protocols_used, 1):
        typer.echo(f"  {i:>4}. {proto}")

    console.print(f"\n  Total protocols: {len(protocols_used)}")
    console.print(f"  Total chunks:    {manifest.total_chunks}")
    console.print(f"  Missing ANY one chunk makes the data unrecoverable.")
    console.print(f"\n  [bold]Platform security is not protocol security.[/bold]")
    console.print(f"{'='*70}\n")


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
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show filesystem metadata for a path."""
    orch = _make_orchestrator(store_dir, db_path)
    m = orch._index.get_manifest_by_path(virtual_path)
    if m is None:
        typer.echo(f"not found: {virtual_path}", err=True)
        raise typer.Exit(1)
    if as_json:
        data = {
            "path": m.path,
            "type": m.file_type,
            "mode": oct(m.mode),
            "size": m.original_size,
            "version": m.version,
            "uid": m.uid,
            "gid": m.gid,
            "mtime": m.mtime,
            "ctime": m.ctime,
            "atime": m.atime,
            "manifest_id": m.manifest_id,
        }
        if m.file_type == "symlink":
            data["symlink_target"] = m.symlink_target
        typer.echo(json_mod.dumps(data, indent=2))
        return
    console.print(f"[dim]  Path:[/dim]    {m.path}")
    console.print(f"[dim]  Type:[/dim]    {m.file_type}")
    console.print(f"[dim]  Mode:[/dim]    {oct(m.mode)}")
    console.print(f"[dim]  Size:[/dim]    {m.original_size}")
    console.print(f"[dim]  Version:[/dim] {m.version}")
    console.print(f"[dim]  UID/GID:[/dim] {m.uid}/{m.gid}")
    console.print(f"[dim]  mtime:[/dim]   {m.mtime}")
    console.print(f"[dim]  ctime:[/dim]   {m.ctime}")
    if m.file_type == "symlink":
        console.print(f"[dim]  Target:[/dim]  {m.symlink_target}")


@app.command()
def tree(
    virtual_path: str = typer.Argument("/", help="Root path for tree."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show directory tree."""
    orch = _make_orchestrator(store_dir, db_path)
    result = asyncio.run(orch.tree(virtual_path))
    if as_json:
        typer.echo(json_mod.dumps(result, indent=2))
        return
    _print_tree_rich(result, "")


def _print_tree_rich(node: dict, prefix: str, is_last: bool = True) -> None:
    name = node["path"].rsplit("/", 1)[-1] or "/"
    if not prefix:
        if "children" in node:
            console.print(f"[blue]{name}/[/blue]")
        else:
            console.print(name)
    else:
        connector = "└── " if is_last else "├── "
        if "children" in node:
            console.print(f"{prefix}{connector}[blue]{name}/[/blue]")
        else:
            console.print(f"{prefix}{connector}{name}")
    children = node.get("children", [])
    for i, child in enumerate(children):
        child_is_last = i == len(children) - 1
        if prefix:
            ext = "    " if is_last else "│   "
        else:
            ext = ""
        _print_tree_rich(child, prefix + ext, child_is_last)


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
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Check filesystem integrity."""
    from picard.fsck import fsck as _fsck

    pp = None
    if keyfile:
        pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path, keyfile=keyfile, passphrase=pp)
    with _spinner("Checking filesystem integrity...") as progress:
        progress.add_task("Checking filesystem integrity...", total=None)
        report = asyncio.run(_fsck(
            orch._index, orch._adapters,
            verify_digests=not no_digests,
            key_ring=orch._key_ring,
        ))
    if as_json:
        data = {
            "total_manifests": report.total_manifests,
            "total_blocks": report.total_blocks,
            "total_chunks": report.total_chunks,
            "verified_chunks": report.verified_chunks,
            "missing_chunks": report.missing_chunks,
            "corrupt_blocks": report.corrupt_blocks,
            "orphan_chunks": report.orphan_chunks,
            "directory_errors": report.directory_errors,
            "symlink_errors": report.symlink_errors,
            "version_gaps": report.version_gaps,
            "journal_incomplete": report.journal_incomplete,
            "errors": report.errors,
            "ok": report.ok,
        }
        typer.echo(json_mod.dumps(data, indent=2))
        return
    console.print(f"fsck: {report.total_manifests} manifests, "
                   f"{report.total_blocks} blocks, {report.total_chunks} chunks")
    console.print(f"  Verified:    {report.verified_chunks}")
    if report.missing_chunks:
        console.print(f"  [red]Missing:     {report.missing_chunks}[/red]")
    if report.corrupt_blocks:
        console.print(f"  [red]Corrupt:     {report.corrupt_blocks}[/red]")
    if report.directory_errors:
        console.print(f"  [red]Dir errors:  {report.directory_errors}[/red]")
    if report.symlink_errors:
        console.print(f"  [red]Link errors: {report.symlink_errors}[/red]")
    if report.journal_incomplete:
        console.print(f"  [yellow]Journal:     {report.journal_incomplete} incomplete[/yellow]")
    for err in report.errors:
        console.print(f"  [red]![/red] {err}")
    if report.ok:
        console.print("  [bold green]OK[/bold green] — no errors found")
    else:
        raise typer.Exit(1)


@app.command()
def log(
    virtual_path: str = typer.Argument(..., help="Path to show history for."),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Show version history for a path."""
    orch = _make_orchestrator(store_dir, db_path)
    versions = asyncio.run(orch.list_versions(virtual_path))
    if as_json:
        typer.echo(json_mod.dumps(versions, indent=2))
        return
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
