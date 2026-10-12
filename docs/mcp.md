# MCP Server — Agent Storage Interface

PICARD exposes scattered encrypted storage as [MCP](https://modelcontextprotocol.io) tools,
letting any agent that speaks MCP store and retrieve data through PICARD's
encrypt-chunk-scatter pipeline. Your data is encrypted with AES-256-GCM,
split into blocks, and scattered across whatever protocol adapters are configured.

## Expectations

**Your data might survive. It might not.** This is not S3. Adapters are protocol
gaps — social media posts get moderated, DNS records expire, MQTT topics get
cleaned, free-tier quotas run out. The filesystem adapter is reliable for local
work. Everything else is "maybe."

- **Writes:** Usually work. Network adapters depend on credentials and rate limits.
- **Reads:** Work if the chunks are still where you left them. Use `picard_verify` first.
- **Speed:** Argon2id key derivation takes ~100ms per 64KB block. Plan accordingly.
- **Concurrency:** SQLite-backed index. One writer at a time.

## Setup

Install with the MCP extra:

```bash
pip install -e ".[mcp]"
```

### Claude Code / Claude Desktop

Add to your MCP config (`claude_desktop_config.json` or `.claude/settings.json`):

```json
{
  "mcpServers": {
    "picard-store": {
      "command": "picard-mcp",
      "env": {
        "PICARD_PASSPHRASE": "your-passphrase-here"
      }
    }
  }
}
```

### Direct invocation

```bash
PICARD_PASSPHRASE=my-secret picard-mcp
```

Or:

```bash
PICARD_PASSPHRASE=my-secret python -m picard.mcp_server
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PICARD_PASSPHRASE` | Yes | — | Encryption passphrase for all read/write operations |
| `PICARD_STORE` | No | `~/.picard/chunks` | Chunk storage directory |
| `PICARD_DB` | No | `~/.picard/index.db` | Index database path |
| `PICARD_INDEX_PASSPHRASE` | No | — | Passphrase for encrypted index database |

The server loads adapters from `~/.picard/picard.toml` if it exists,
falling back to a single filesystem adapter.

## Tools

### picard_put

Store content at a virtual path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Virtual path (e.g., `/agent/notes/today.md`) |
| `content` | string | Yes | Content to store |
| `encoding` | string | No | `utf-8` (default) or `base64` for binary |

Returns: `{status, path, size, blocks, version, manifest_id}`

Parent directories are created automatically.

### picard_get

Retrieve content from a virtual path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Virtual path to retrieve |
| `encoding` | string | No | `utf-8` (default) or `base64` for binary |

Returns: the stored content as text.

### picard_ls

List contents of a directory.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | No | Directory path (default: `/`) |

Returns: `[{path, name, type, size, version}, ...]`

### picard_rm

Delete a file and its chunks from all adapters. Permanent.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Virtual path to delete |

Returns: `{status: "deleted"|"not_found", path}`

### picard_mkdir

Create a virtual directory (parents created automatically).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Directory path to create |

### picard_stat

Get metadata about a stored path.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Virtual path |

Returns: `{path, type, size, version, blocks, mode, mtime, ctime, parent, manifest_id}`

### picard_tree

Get a directory tree as a nested structure.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | No | Root path (default: `/`) |

### picard_verify

Check if a file's chunks are still intact on their adapters.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `path` | string | Yes | Virtual path to verify |

Returns: `{path, ok, verified_chunks, missing_chunks, corrupt_blocks}`

## Usage Patterns

### Agent scratch storage

```
picard_mkdir("/agent/scratch")
picard_put("/agent/notes.md", session_notes)
later: picard_get("/agent/notes.md")
```

### Namespace by session or task

```
picard_mkdir("/sessions/abc123")
picard_put("/sessions/abc123/context.json", data)
```

### Verify before trusting

```
verify = picard_verify("/important/data.csv")
if verify.ok:
    data = picard_get("/important/data.csv")
```

## What NOT to Store

- **Secrets or credentials** — the passphrase is in an env var and paths are cleartext in the index
- **Files over ~100MB** — Argon2id per block adds up; use the CLI's streaming mode for large files
- **Anything you can't afford to lose** — see "Expectations" above
