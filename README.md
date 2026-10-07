# PICARD — Protocol-Introspecting Chunk Adapter for Resilient Data

> A push for protocol security and away from platforms for the integrity of compute.

PICARD encrypts your data with AES-256-GCM, splits it into blocks, chunks each block,
and scatters the chunks across 259 protocol adapters — from social media APIs to DNS
TXT records to Bluetooth beacons. No single adapter holds enough to reconstruct anything.

## Features

- **259 protocol adapters** across 15+ categories (social, network, storage, P2P, exotic)
- **AES-256-GCM encryption** with Argon2id key derivation (per-block salt + nonce)
- **RAID redundancy** — levels 0/1/5/10 across adapter pools
- **Scatter profiles** — stealth, resilient, speed, paranoid (config-driven)
- **Full filesystem** — directories, symlinks, versioning, partial reads
- **FUSE mount** — mount as a real filesystem (Linux/macOS/Windows)
- **Block cache** — LRU with TTL for fast repeated reads
- **Journaling** — crash recovery via write-ahead log
- **fsck** — integrity checking, orphan detection, repair
- **Streaming I/O** — handle files larger than RAM
- **Encrypted index** — AES-256-GCM encrypted SQLite at rest
- **Network transport** — peer-to-peer via HTTP with HMAC auth
- **Key hierarchy** — HKDF sub-keys per adapter with version rotation
- **Sealed envelopes** — no plaintext PCHK visible on the wire
- **CLI** — 30+ commands with progress bars, --json output, colored display
- **Web dashboard** — browser-based adapter management and file browser
- **MCP server** — agent-accessible storage tools for any MCP-compatible client
- **Tor integration** — route traffic through Tor, expose as hidden service, connect to .onion peers

## Quickstart

```bash
pip install -e ".[dev]"

picard put myfile.txt --as /docs/myfile.txt
picard get /docs/myfile.txt -o recovered.txt
picard ls
picard tree /
picard mount /mnt/picard    # requires libfuse/WinFSP/macFUSE
```

## Architecture

```
plaintext --> [AES-256-GCM per block] --> [PCHK chunking] --> [scatter across adapters]
                    |                                                  |
              Argon2id KDF                                   RAID pool / envelope seal
              (per-block salt+nonce)                         (HKDF sub-keys per adapter)
```

Each block is independently decryptable with its own random salt (16 bytes) and nonce (12 bytes).
Chunks are scattered round-robin across adapters. RAID pools provide redundancy.
The sealed envelope format wraps chunks with per-adapter sub-keys and correlation tags,
producing output indistinguishable from random data.

## Adapter Categories

| Category | Count | Examples |
|----------|-------|---------|
| Social / Messaging | 12 | Bluesky, Reddit, Discord, Telegram, Matrix, Slack |
| Network Protocols | 20 | DNS, MQTT, gRPC, WebSocket, CoAP, TFTP, UDP |
| Storage / KV | 11 | S3, Redis, PostgreSQL, DynamoDB, CouchDB |
| Mail / Calendar | 4 | SMTP/IMAP, CalDAV, CardDAV, WebDAV |
| P2P / Exotic | 10 | IPFS, BitTorrent DHT, Ethereum, QR codes, Steganography |
| Format / Stealth | 7+ | XLSX hidden cells, EXIF metadata, PDF, ZIP comments |
| Broadcast / Discovery | 10+ | mDNS, SSDP, BLE, NFC, WiFi beacons |
| ... | ... | 180+ more across every protocol gap we could find |

## Configuration

```toml
# picard.toml
[general]
block_size = 65536

[[adapters]]
name = "s3-chunks"
type = "s3"
enabled = true
params = { bucket = "picard-chunks", prefix = "data/" }

[[adapters]]
name = "redis-cache"
type = "redis"
enabled = true
params = { url = "redis://localhost" }

[[raid]]
level = "raid5"
adapters = ["s3-chunks", "redis-cache", "filesystem"]

[scatter.paranoid]
raid_level = "raid10"
min_adapters = 4
```

## CLI Commands

### Core
- `picard put` / `get` / `ls` / `rm` — basic file operations
- `picard cp` / `mv` — copy and rename
- `picard info` / `stat` — metadata inspection
- `picard verify` — chunk integrity check
- `picard export` / `import` — manifest bundles

### Filesystem
- `picard mkdir` / `rmdir` — directory management (via `rm`)
- `picard tree` / `find` — directory browsing
- `picard ln` — symbolic links
- `picard head` / `tail` — partial reads
- `picard touch` — create or update timestamps
- `picard log` — version history
- `picard fsck` — filesystem consistency check
- `picard mount` — FUSE mount

### Infrastructure
- `picard web` — launch web dashboard
- `picard recover` — journal crash recovery
- `picard demo` — scatter across all adapters
- `picard scatter-report` — show attack surface map
- `picard adapters` / `profiles` — list available adapters and scatter profiles
- `picard init` — create default config.toml
- `picard encrypt-index` / `decrypt-index` — encrypt or decrypt the index database
- `picard tor-check` — verify Tor connectivity

Most commands support `--json` for machine-readable output.

## Documentation

- [Architecture](docs/architecture.md) — data flow, key hierarchy, RAID, scatter
- [Adapters](docs/adapters.md) — adapter list, writing custom adapters
- [CLI Reference](docs/cli.md) — all commands with examples
- [MCP Server](docs/mcp.md) — agent storage interface, setup, tools
- [Network](docs/network.md) — transport layer, peer discovery, remote adapters
- [FUSE Mount](docs/fuse.md) — mounting, performance, platform notes
- [Tor](docs/tor.md) — anonymity, hidden services, .onion peers

## Development

```bash
pip install -e ".[dev]"
python -m pytest --tb=short -q
```

Python 3.11+ required. Core dependencies: `cryptography`, `argon2-cffi`, `typer`, `rich`.

## License

MIT
