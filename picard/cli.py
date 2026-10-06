"""CLI — ``picard put / get / ls / rm``.

A thin shell over the orchestrator that wires up adapters, index,
and passphrase handling.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from picard.adapters.filesystem import FilesystemAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator

app = typer.Typer(
    name="picard",
    help="Scatter encrypted data across protocol gaps.",
    add_completion=False,
)

# Defaults — override via env vars or future config file.
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


def _make_orchestrator(
    store_dir: Path = _DEFAULT_STORE,
    db_path: Path = _DEFAULT_DB,
) -> Orchestrator:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store_dir.mkdir(parents=True, exist_ok=True)
    adapter = FilesystemAdapter(store_dir)
    index = Index(db_path)
    return Orchestrator([adapter], index)


@app.command()
def put(
    file: Path = typer.Argument(..., help="Local file to store."),
    virtual_path: str = typer.Option(
        "", "--as", "-a", help="Virtual path (default: filename)."
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
    orch = _make_orchestrator(store_dir, db_path)
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
    store_dir: Path = typer.Option(_DEFAULT_STORE, "--store", envvar="PICARD_STORE"),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", envvar="PICARD_DB"),
) -> None:
    """Retrieve and decrypt a file."""
    pp = _get_passphrase()
    orch = _make_orchestrator(store_dir, db_path)
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


if __name__ == "__main__":
    app()
