"""PICARD MCP server — expose scattered storage as agent tools.

Run with: picard-mcp
Or: python -m picard.mcp_server

Expects PICARD_PASSPHRASE in environment. Without it, all writes fail.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from picard.adapters.filesystem import FilesystemAdapter
from picard.index import Index
from picard.journal import Journal
from picard.orchestrator import Orchestrator

_DEFAULT_STORE = Path(os.environ.get("PICARD_STORE", Path.home() / ".picard" / "chunks"))
_DEFAULT_DB = Path(os.environ.get("PICARD_DB", Path.home() / ".picard" / "index.db"))

_orch: Orchestrator | None = None
_index: Index | None = None


def _passphrase() -> str:
    pp = os.environ.get("PICARD_PASSPHRASE", "")
    if not pp:
        raise RuntimeError(
            "PICARD_PASSPHRASE not set. "
            "Set it in your environment before starting the MCP server."
        )
    return pp


def _get_orchestrator() -> tuple[Orchestrator, Index]:
    global _orch, _index
    if _orch is not None and _index is not None:
        return _orch, _index

    _DEFAULT_STORE.mkdir(parents=True, exist_ok=True)
    _DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)

    idx_pp = os.environ.get("PICARD_INDEX_PASSPHRASE") or None
    idx = Index(_DEFAULT_DB, passphrase=idx_pp)

    adapters = [FilesystemAdapter(_DEFAULT_STORE)]

    config_path = Path.home() / ".picard" / "picard.toml"
    if config_path.exists():
        try:
            from picard.config import load_config
            cfg = load_config(config_path)
            if cfg.get("adapters"):
                from picard.registry import build_adapters
                adapters = build_adapters(cfg["adapters"])
        except Exception:
            pass

    journal = Journal(idx.connection)
    _orch = Orchestrator(adapters, idx, journal=journal)
    _index = idx
    return _orch, idx


TOOLS = [
    Tool(
        name="picard_put",
        description=(
            "Store content at a virtual path in PICARD's scattered encrypted storage. "
            "Data is encrypted with AES-256-GCM, chunked, and scattered across protocol adapters. "
            "Expect ~100ms per 64KB block for key derivation. Your data might survive. It might not."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Virtual path (e.g., '/agent/notes/today.md')",
                },
                "content": {
                    "type": "string",
                    "description": "Content to store",
                },
                "encoding": {
                    "type": "string",
                    "enum": ["utf-8", "base64"],
                    "default": "utf-8",
                    "description": "Content encoding. Use base64 for binary data.",
                },
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="picard_get",
        description=(
            "Retrieve content from a virtual path. Returns the stored content, "
            "decrypted and reassembled from scattered chunks. "
            "Returns an error if the path doesn't exist or chunks are missing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Virtual path to retrieve",
                },
                "encoding": {
                    "type": "string",
                    "enum": ["utf-8", "base64"],
                    "default": "utf-8",
                    "description": "Output encoding. Use base64 for binary data.",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="picard_ls",
        description=(
            "List contents of a virtual directory. Returns entries with path, type, and size."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/",
                    "description": "Directory path to list",
                },
            },
        },
    ),
    Tool(
        name="picard_rm",
        description=(
            "Delete a file and its chunks from all adapters. "
            "This is permanent — scattered chunks are removed from every adapter that holds them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Virtual path to delete",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="picard_mkdir",
        description="Create a virtual directory. Creates parent directories automatically.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to create",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="picard_stat",
        description=(
            "Get metadata about a stored path: size, type (regular/directory/symlink), "
            "version, timestamps, mode."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Virtual path to inspect",
                },
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="picard_tree",
        description="Get a directory tree as a nested structure.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "default": "/",
                    "description": "Root path for the tree",
                },
            },
        },
    ),
    Tool(
        name="picard_verify",
        description=(
            "Check if a file's chunks are still intact on their adapters. "
            "Returns chunk-level integrity status. Use this before trusting a read."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Virtual path to verify",
                },
            },
            "required": ["path"],
        },
    ),
]

server = Server("picard-store")


@server.list_tools()
async def list_tools():
    return TOOLS


def _sanitize_path(path: str) -> str:
    """Normalize a virtual path: collapse ``..``, strip nulls."""
    path = path.replace("\x00", "")
    import posixpath
    path = posixpath.normpath(path)
    if path == ".":
        path = "/"
    return path


def _validate_path(path: str) -> str | None:
    """Return an error message if *path* is invalid, else None."""
    if not path:
        return "path must not be empty"
    if "\x00" in path:
        return "path must not contain null bytes"
    if len(path) > 4096:
        return "path must not exceed 4096 characters"
    return None


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        orch, index = _get_orchestrator()
    except Exception as e:
        return [TextContent(type="text", text=f"error: {e}")]

    try:
        _PATH_TOOLS = {
            "picard_put", "picard_get", "picard_rm",
            "picard_mkdir", "picard_stat", "picard_verify",
            "picard_ls", "picard_tree",
        }
        if name in _PATH_TOOLS and "path" in arguments:
            arguments["path"] = _sanitize_path(arguments["path"])
        _PATH_REQUIRED = {
            "picard_put", "picard_get", "picard_rm",
            "picard_mkdir", "picard_stat", "picard_verify",
        }
        if name in _PATH_REQUIRED:
            err = _validate_path(arguments.get("path", ""))
            if err:
                return [TextContent(type="text", text=f"error: {err}")]

        if name == "picard_put":
            path = arguments["path"]
            content = arguments["content"]
            encoding = arguments.get("encoding", "utf-8")

            if encoding == "base64":
                data = base64.b64decode(content)
            else:
                data = content.encode("utf-8")

            pp = _passphrase()
            parent = "/".join(path.split("/")[:-1]) or "/"
            if parent != "/":
                try:
                    await orch.mkdir(parent, parents=True)
                except Exception:
                    pass

            manifest = await orch.put(path, data, pp)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "status": "stored",
                    "path": manifest.path,
                    "size": manifest.original_size,
                    "blocks": manifest.total_blocks,
                    "version": manifest.version,
                    "manifest_id": manifest.manifest_id,
                }),
            )]

        elif name == "picard_get":
            path = arguments["path"]
            encoding = arguments.get("encoding", "utf-8")
            pp = _passphrase()
            data = await orch.get(path, pp)

            if encoding == "base64":
                content = base64.b64encode(data).decode("ascii")
            else:
                content = data.decode("utf-8", errors="replace")

            return [TextContent(type="text", text=content)]

        elif name == "picard_ls":
            path = arguments.get("path", "/")
            children = index.list_children_with_metadata(path)
            entries = [
                {
                    "path": c["path"],
                    "name": c["name"],
                    "type": c["type"],
                    "size": c["size"],
                    "version": c["version"],
                }
                for c in children
            ]
            return [TextContent(type="text", text=json.dumps(entries, indent=2))]

        elif name == "picard_rm":
            path = arguments["path"]
            ok = await orch.rm(path)
            status = "deleted" if ok else "not_found"
            return [TextContent(type="text", text=json.dumps({"status": status, "path": path}))]

        elif name == "picard_mkdir":
            path = arguments["path"]
            await orch.mkdir(path, parents=True)
            return [TextContent(type="text", text=json.dumps({"status": "created", "path": path}))]

        elif name == "picard_stat":
            path = arguments["path"]
            m = index.get_manifest_by_path(path)
            if m is None:
                return [TextContent(type="text", text=json.dumps({"error": "not found", "path": path}))]
            return [TextContent(type="text", text=json.dumps({
                "path": m.path,
                "type": m.file_type,
                "size": m.original_size,
                "version": m.version,
                "blocks": m.total_blocks,
                "mode": oct(m.mode),
                "mtime": m.mtime,
                "ctime": m.ctime,
                "parent": m.parent_path,
                "manifest_id": m.manifest_id,
            }))]

        elif name == "picard_tree":
            path = arguments.get("path", "/")
            tree = await orch.tree(path)
            return [TextContent(type="text", text=json.dumps(tree, indent=2))]

        elif name == "picard_verify":
            path = arguments["path"]
            report = await orch.verify(path)
            return [TextContent(type="text", text=json.dumps({
                "path": path,
                "ok": report.get("ok", False),
                "verified_chunks": report.get("verified_chunks", 0),
                "missing_chunks": report.get("missing_chunks", 0),
                "corrupt_blocks": report.get("corrupt_blocks", 0),
            }))]

        else:
            return [TextContent(type="text", text=f"error: unknown tool '{name}'")]

    except Exception as e:
        return [TextContent(type="text", text=f"error: {type(e).__name__}: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
