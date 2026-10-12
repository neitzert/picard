# WebDAV Server

PICARD includes a WebDAV server that lets you mount the encrypted filesystem
as a network drive in your OS file manager — Explorer, Finder, or Nautilus.
Drag-and-drop files in, and they're automatically encrypted, chunked, and
scattered across your configured adapters.

## Quickstart

```bash
pip install -e ".[webdav-server]"
picard webdav
```

The server starts on `http://127.0.0.1:8472` by default.

## Options

```
picard webdav [OPTIONS]

  --host TEXT              Bind address (default: 127.0.0.1)
  --port INT               Port (default: 8472)
  --passphrase TEXT        Encryption passphrase (or set PICARD_PASSPHRASE)
  --index-passphrase TEXT  Index DB passphrase (or set PICARD_INDEX_PASSPHRASE)
  --read-only              Block all write operations
```

## Connecting

### Windows

1. Open File Explorer
2. Right-click "This PC" → "Map Network Drive"
3. Enter `http://localhost:8472` as the folder
4. Or from cmd: `net use Z: http://localhost:8472`

### macOS

1. Finder → Go → Connect to Server (Cmd+K)
2. Enter `http://localhost:8472`

### Linux

```bash
# GNOME/Nautilus
gio mount dav://localhost:8472

# KDE/Dolphin — enter in address bar:
webdav://localhost:8472

# Command line (davfs2)
sudo mount -t davfs http://localhost:8472 /mnt/picard
```

## Security

- Binds to `127.0.0.1` by default — localhost only
- For remote access, use an SSH tunnel: `ssh -L 8472:localhost:8472 user@host`
- Read-only mode prevents accidental modifications
- All data is encrypted before touching any adapter

## How It Works

The WebDAV server maps standard WebDAV methods to PICARD orchestrator operations:

| WebDAV Method | PICARD Operation |
|---------------|-----------------|
| PROPFIND      | ls() / stat()   |
| GET           | get()           |
| PUT           | put()           |
| DELETE        | rm()            |
| MKCOL         | mkdir()         |
| MOVE          | mv()            |
| COPY          | cp()            |

Files written via WebDAV go through the same encrypt-chunk-scatter pipeline
as the CLI and web dashboard.
