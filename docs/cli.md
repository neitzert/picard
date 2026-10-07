# CLI Reference

PICARD's CLI is built with Typer. All commands use Rich for colored output and
progress indicators. Most commands support `--json` for machine-readable output.

## Global Options

All commands that access stored files accept:

- `--store PATH` — chunk storage directory (default: `~/.picard/chunks`)
- `--db PATH` — index database path (default: `~/.picard/index.db`)
- `--keyfile PATH` — master key file for envelope sealing
- `--profile NAME` — scatter profile (stealth, resilient, speed, paranoid, or custom)
- `--index-passphrase TEXT` — passphrase for encrypted index database (or set `PICARD_INDEX_PASSPHRASE` env var)

## Core Commands

### put

Store a file by encrypting, chunking, and scattering it.

```bash
picard put myfile.txt --as /docs/myfile.txt
picard put largefile.bin --as /data/large.bin --profile resilient
```

Prompts for a passphrase. Shows a progress spinner during encryption and scatter.

### get

Retrieve and decrypt a stored file.

```bash
picard get /docs/myfile.txt -o recovered.txt
picard get /docs/myfile.txt    # writes to stdout
```

### ls

List stored virtual paths.

```bash
picard ls                   # all paths
picard ls --json            # JSON array with path, type, size, version
```

Colored output: directories in blue, symlinks in cyan with target.

### rm

Delete a file's chunks from adapters and remove its manifest.

```bash
picard rm /docs/myfile.txt
```

### info

Detailed metadata about a stored file.

```bash
picard info /docs/myfile.txt
picard info /docs/myfile.txt --json
```

### verify

Check integrity of all chunks for a file.

```bash
picard verify /docs/myfile.txt
```

Reports verified chunks, missing chunks, corrupt blocks.

### cp / mv

Copy or rename a virtual file.

```bash
picard cp /docs/a.txt /backup/a.txt
picard mv /docs/old.txt /docs/new.txt
```

`cp` re-indexes without re-scattering (zero data movement).

### du

Storage usage summary per adapter.

```bash
picard du
picard du --json
```

### export / import

Export a manifest bundle (for sharing) or import one.

```bash
picard export /docs/myfile.txt -o bundle.json
picard import bundle.json
```

## Filesystem Commands

### mkdir

Create a virtual directory.

```bash
picard mkdir /docs
picard mkdir /docs/sub/dir --parents   # create intermediate directories
```

### stat

Show filesystem metadata for a path.

```bash
picard stat /docs/myfile.txt
picard stat /docs/myfile.txt --json
```

Shows: path, type, size, mode, timestamps, version, parent.

### tree

Display directory tree with box-drawing characters.

```bash
picard tree /
picard tree /docs --json
```

### find

Find paths matching a prefix.

```bash
picard find /docs
```

### touch

Create an empty file or update timestamps.

```bash
picard touch /docs/new.txt
```

### ln

Create a symbolic link.

```bash
picard ln /docs/link.txt /docs/target.txt
```

### head / tail

Read the first or last N bytes of a file.

```bash
picard head /docs/myfile.txt -n 1024
picard tail /docs/myfile.txt -n 512
```

### log

Show version history for a path.

```bash
picard log /docs/myfile.txt
picard log /docs/myfile.txt --json
```

### fsck

Check filesystem consistency.

```bash
picard fsck
picard fsck --json
```

Checks: chunk integrity, directory structure, symlink targets, journal entries.

### mount

Mount PICARD as a FUSE filesystem.

```bash
picard mount /mnt/picard
picard mount P:                    # Windows drive letter
picard mount /mnt/picard --readonly
```

Requires libfuse (Linux), macFUSE (macOS), or WinFSP (Windows).

## Infrastructure Commands

### web

Launch the web dashboard with dark/light theme, drag-and-drop uploads, storage health
monitoring, per-adapter Tor/VPN routing, and browser mode configuration.

```bash
picard web                    # http://localhost:8471
picard web --port 9000
```

### webdav

Mount PICARD as a WebDAV network drive for native OS drag-and-drop.

```bash
picard webdav                           # http://localhost:8472
picard webdav --port 8472 --read-only
picard webdav --passphrase secret
```

Connect from your OS file manager:
- **Windows:** Map Network Drive → `http://localhost:8472`
- **macOS:** Finder → Go → Connect to Server → `http://localhost:8472`
- **Linux:** `gio mount dav://localhost:8472` or Nautilus address bar

### recover

Clean up incomplete operations from crashed puts.

```bash
picard recover
```

Uses the journal to find incomplete entries and clean up orphan chunks.

### demo

Scatter a test payload across all configured adapters.

```bash
picard demo
picard demo --profile paranoid
```

### scatter-report

Show the storage surface map — how data is distributed.

```bash
picard scatter-report /docs/myfile.txt
```

### adapters

List all registered adapter types.

```bash
picard adapters
picard adapters --json
```

### profiles

List available scatter profiles.

```bash
picard profiles
picard profiles --json
```

### init

Create a default `picard.toml` configuration file.

```bash
picard init
```

### encrypt-index

Encrypt an existing plain SQLite index database with AES-256-GCM (PCIX format).

```bash
picard encrypt-index
picard encrypt-index --db /path/to/index.db
```

Prompts for a passphrase interactively.

### decrypt-index

Decrypt an encrypted index database back to plain SQLite.

```bash
picard decrypt-index
picard decrypt-index --db /path/to/index.db
```

## JSON Output

Commands with `--json` produce structured output suitable for piping:

```bash
picard ls --json | jq '.[].path'
picard stat /docs/myfile.txt --json | jq '.version'
picard adapters --json | jq 'length'
```

## Common Workflows

### Store and Retrieve

```bash
picard put secret.pdf --as /vault/secret.pdf
# Enter passphrase...
picard get /vault/secret.pdf -o decrypted.pdf
# Enter same passphrase...
```

### Mount and Browse

```bash
picard mount /mnt/picard
ls /mnt/picard/
cat /mnt/picard/vault/secret.pdf
echo "new file" > /mnt/picard/notes.txt
fusermount -u /mnt/picard
```

### Check Integrity

```bash
picard fsck
picard verify /vault/secret.pdf
picard recover    # if fsck finds journal issues
```

### Use RAID Redundancy

```bash
picard put important.db --as /db/main.db --profile resilient
picard scatter-report /db/main.db
```
